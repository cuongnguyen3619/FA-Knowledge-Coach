"""FA Knowledge Coach v6 – single submission, batch grading and Sheets writes."""
import hashlib
import json
import random
import re
import uuid
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import streamlit as st
from google.oauth2.service_account import Credentials
from openai import OpenAI

SHEET_ID = "15yQB2hYcTVPZjVNzwuUjUf0pmch2WpmHbN3WPglb3z4"
TZ = ZoneInfo("Asia/Ho_Chi_Minh")
APPROVED = {"approved", "đã duyệt"}
LEVEL_COUNTS = {1: 6, 2: 3, 3: 2, 4: 2}
POINTS = {1: 1, 2: 2, 3: 4, 4: 6}
ASSIGN_HEADERS = ["Session ID", "Học viên", "Ngày", "Trạng thái", "Bắt đầu", "Kết thúc", "Danh sách câu JSON", "Số câu mới", "Số câu ôn", "Điểm mới", "Điểm tối đa", "Cập nhật lúc"]
WARN_HEADERS = ["Học viên", "Question ID", "Số lần không đạt", "Điểm lần gần nhất (%)", "Nội dung cần ôn", "Thời gian", "Trạng thái"]

st.set_page_config(page_title="FA Knowledge Coach", page_icon="🚙", layout="centered")
st.title("FA Knowledge Coach")
st.caption("Ford Academy Vietnam · Daily Assessment")
st.markdown("""<style>
/* Distinct readable surfaces for questions, learner responses, and feedback */
.fa-panel {border:1px solid #bac8d8;border-left:5px solid #1459a4;border-radius:10px;padding:14px 17px;margin:10px 0 14px;background:#edf4fc;color:#142c45;line-height:1.6}
.fa-panel strong {color:#0c3768}
.fa-answer {background:#f2f5f9;border-left-color:#65758a;color:#172b40}
.fa-feedback {background:#eaf7f1;border-left-color:#16784b;color:#153e30}
.fa-reference {background:#fff8e8;border-left-color:#ae7414;color:#4c3410}
@media (prefers-color-scheme: dark) {
.fa-panel {background:#152a42;border-color:#425c79;color:#e8f2ff}
.fa-panel strong {color:#b8dcff}
.fa-answer {background:#26313f;color:#f0f4fa}
.fa-feedback {background:#173b30;color:#e0faec}
.fa-reference {background:#45361b;color:#fff0ca}
}
</style>""", unsafe_allow_html=True)

def panel(label, content, kind="question"):
    import html
    css = {"question":"fa-panel", "answer":"fa-panel fa-answer", "feedback":"fa-panel fa-feedback", "reference":"fa-panel fa-reference"}[kind]
    st.markdown(f'<div class="{css}"><strong>{html.escape(str(label))}</strong><div style="white-space:pre-wrap;margin-top:6px">{html.escape(str(content or "—"))}</div></div>', unsafe_allow_html=True)


def timestamp():
    return datetime.now(TZ).isoformat(timespec="seconds")

def norm(value):
    return str(value or "").strip()

def level(row):
    try:
        return int(float(row.get("Level", 0)))
    except (ValueError, TypeError):
        return 0

def day(row):
    try:
        return int(float(row.get("Ngày nội dung", 0)))
    except (ValueError, TypeError):
        return 0

def approved(row):
    # Level 1 MCQs are eligible regardless of approval status.
    # Levels 2–4 require Trainer approval.
    return level(row) == 1 or norm(row.get("Trạng thái duyệt")).casefold() in APPROVED

@st.cache_resource(show_spinner=False)
def book():
    creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID)

def sheet(name):
    return book().worksheet(name)

@st.cache_data(ttl=300, show_spinner=False)
def records(name):
    # Shared cache prevents a Google Sheets read on every Streamlit rerun.
    return sheet(name).get_all_records(default_blank="")

def ensure_tab(name, headers):
    try:
        return sheet(name)
    except gspread.WorksheetNotFound:
        ws = book().add_worksheet(title=name, rows=2000, cols=max(20, len(headers)))
        ws.append_row(headers, value_input_option="RAW")
        return ws

def question_bank():
    result = {}
    for row in records("Question Bank - L2/3/4"):
        qid = norm(row.get("Question ID"))
        if qid and level(row) in (2, 3, 4):
            result[qid] = dict(row)
    for row in records("Question Bank - L1"):
        qid = norm(row.get("Question ID"))
        if not qid or level(row) != 1:
            continue
        choices = {k: norm(row.get(k)) for k in "ABCD" if norm(row.get(k))}
        keys = sorted(set(re.findall(r"[A-D]", norm(row.get("Đáp án đề gốc")).upper())))
        if choices and keys and all(k in choices for k in keys):
            item = dict(row)
            item["_choices"] = choices
            item["_keys"] = keys
            result[qid] = item
    return result

def stable_sample(items, count, student, selected_day, lev):
    seed = int(hashlib.sha256(f"{student}|{selected_day}|{lev}|FA".encode()).hexdigest(), 16)
    return random.Random(seed).sample(sorted(items), count)

def answers_for(student, history=None):
    history = records("Câu trả lời") if history is None else history
    return [x for x in history if norm(x.get("Họ và tên")) == student and norm(x.get("Question ID"))]

def prior_failures(student, selected_day, bank, history=None):
    # Most recent graded result per question governs the outstanding remediation queue.
    history = answers_for(student, history)
    latest = {}
    for x in history:
        qid = norm(x.get("Question ID"))
        if qid not in bank or day(bank[qid]) >= selected_day:
            continue
        try:
            earned = float(x.get("Điểm đạt"))
            maximum = float(x.get("Điểm tối đa"))
            if maximum <= 0:
                continue
        except (TypeError, ValueError):
            continue
        latest[qid] = earned / maximum
    return sorted(qid for qid, ratio in latest.items() if ratio < 0.5)

def build_assignment(student, selected_day, bank, history=None):
    fresh = []
    missing = {}
    outstanding = set(prior_failures(student, selected_day, bank, history))
    previous = {norm(a.get("Question ID")) for a in answers_for(student, history)}
    for lev, required in LEVEL_COUNTS.items():
        # Cumulative bank: Day N may draw from Days 1..N. Do not count
        # unresolved prior failures as new questions; those are review items.
        eligible = [qid for qid, q in bank.items()
                    if approved(q) and 1 <= day(q) <= selected_day
                    and level(q) == lev and qid not in outstanding
                    and (lev == 1 or norm(q.get("Đáp án tham khảo")))]
        never_seen = [qid for qid in eligible if qid not in previous]
        # Prefer content from the selected day, then earlier days.
        never_seen.sort(key=lambda qid: (-day(bank[qid]), qid))
        chosen = []
        for source in (never_seen, [qid for qid in eligible if qid in previous]):
            for d in range(selected_day, 0, -1):
                group = [qid for qid in source if day(bank[qid]) == d and qid not in chosen]
                take = min(required-len(chosen), len(group))
                if take:
                    chosen.extend(stable_sample(group, take, student, selected_day, lev))
                if len(chosen) == required:
                    break
            if len(chosen) == required:
                break
        if len(chosen) < required:
            missing[lev] = (len(chosen), required)
        else:
            fresh.extend(chosen)
    if missing:
        return None, missing
    repeat = [qid for qid in sorted(outstanding) if qid not in fresh]
    order = [{"id": qid, "review": False} for qid in fresh] + [{"id": qid, "review": True} for qid in repeat]
    seed = int(hashlib.sha256(f"{student}|{selected_day}|order".encode()).hexdigest(), 16)
    random.Random(seed).shuffle(order)
    return order, {}

def assignment_row(student, selected_day, assignments=None):
    assignments = records("Bài kiểm tra") if assignments is None else assignments
    for row in assignments:
        if norm(row.get("Học viên")) == student and norm(row.get("Ngày")) == str(selected_day):
            return row
    return None

def start_assignment(student, selected_day, order):
    sid = str(uuid.uuid4())
    ensure_tab("Bài kiểm tra", ASSIGN_HEADERS).append_row([
        sid, student, selected_day, "Đang làm", timestamp(), "", json.dumps(order, ensure_ascii=False),
        13, len(order)-13, "", 32, timestamp()], value_input_option="RAW")
    return sid

def update_assignment(sid, **values):
    ws = ensure_tab("Bài kiểm tra", ASSIGN_HEADERS)
    cells = ws.get_all_values()
    for index, row in enumerate(cells[1:], 2):
        if row and row[0] == sid:
            for key, value in values.items():
                ws.update_cell(index, ASSIGN_HEADERS.index(key)+1, value)
            return
    raise RuntimeError("Không tìm thấy bài kiểm tra để cập nhật")

def grade_open(question, answer, max_points):
    key = st.secrets.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("Chưa có OPENAI_API_KEY trong Streamlit Secrets")
    if not norm(question.get("Đáp án tham khảo")):
        raise RuntimeError("Câu hỏi thiếu đáp án tham khảo; Trainer cần bổ sung")
    client = OpenAI(api_key=key)
    completion = client.chat.completions.create(
        model=st.secrets.get("OPENAI_MODEL", "gpt-4.1-mini"),
        temperature=0, response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Bạn là giám khảo Ford Academy. Chỉ dựa trên câu hỏi và đáp án tham khảo của Trainer. "
                "Đánh giá các ý đúng, thiếu, sai và mức độ hoàn thành; chấp nhận cách diễn đạt tương đương. "
                "Không đòi hỏi rubric, không thêm tiêu chí hoặc dữ kiện ngoài đáp án tham khảo. "
                "Trả JSON: ratio (0..1), correct_points (array strings), missing_points (array strings), "
                "incorrect_points (array strings), feedback (string), needs_review (boolean). "
                "Nếu nguồn không đủ hoặc mơ hồ, needs_review=true."
            )},
            {"role": "user", "content": json.dumps({
                "question": question.get("Câu hỏi"), "reference": question.get("Đáp án tham khảo"),
                "answer": answer
            }, ensure_ascii=False)}
        ])
    data = json.loads(completion.choices[0].message.content)
    ratio = min(1.0, max(0.0, float(data.get("ratio", 0))))
    return round(ratio * max_points, 2), data

def save_answer(sid, student, selected_day, q, response, score, maximum, feedback, review):
    sheet("Câu trả lời").append_row([
        sid, student, selected_day, q.get("Question ID"), q.get("Ngày nội dung"),
        q.get("Module"), level(q), q.get("Câu hỏi"), response,
        q.get("Đáp án tham khảo") or q.get("Đáp án đề gốc"), json.dumps(feedback, ensure_ascii=False),
        score, maximum, "Cần Trainer kiểm duyệt" if review else ("Đạt" if score/maximum >= 0.5 else "Chưa đạt"),
        "; ".join(feedback.get("missing_points", [])), q.get("Trang nguồn") or q.get("File nguồn"),
        timestamp(), "Pending Trainer review" if review else "Auto graded",
    ], value_input_option="RAW")

def previous_results(sid):
    return {norm(x.get("Question ID")): x for x in records("Câu trả lời") if norm(x.get("Session ID")) == sid}

def trainer_warnings(student, history=None):
    failed = {}
    for x in answers_for(student, history):
        try:
            score, maximum = float(x.get("Điểm đạt")), float(x.get("Điểm tối đa"))
            if maximum <= 0 or norm(x.get("Trạng thái kiểm duyệt")).lower().startswith("pending"):
                continue
        except (TypeError, ValueError):
            continue
        if score / maximum < 0.5:
            failed.setdefault(norm(x.get("Question ID")), []).append(x)
    return {qid: attempts for qid, attempts in failed.items() if len(attempts) >= 2}

def show_final_report(order, completed, bank):
    """Evidence-based end-of-test feedback; no extra Google Sheets calls or API cost."""
    fresh = [(bank.get(item["id"], {}), completed[item["id"]]) for item in order
             if not item.get("review") and item["id"] in completed]
    if not fresh:
        st.info("Chưa có dữ liệu chấm điểm để lập nhận xét.")
        return
    total = sum(float(a.get("Điểm đạt") or 0) for _, a in fresh)
    st.metric("Tổng điểm bài đánh giá", f"{total:g}/32", f"{total / 32:.0%}")
    st.subheader("Đánh giá theo cấp độ")
    weak = []
    strengths = []
    for lev in (1, 2, 3, 4):
        rows = [(q, a) for q, a in fresh if level(q) == lev]
        got = sum(float(a.get("Điểm đạt") or 0) for _, a in rows)
        maximum = sum(float(a.get("Điểm tối đa") or POINTS[lev]) for _, a in rows)
        ratio = got / maximum if maximum else 0
        st.write(f"**Level {lev}: {got:g}/{maximum:g} điểm ({ratio:.0%})**")
        if ratio >= .8:
            strengths.append(f"Level {lev}")
        elif ratio < .5:
            weak.append(f"Level {lev}")
    st.subheader("Nhận xét và lời khuyên")
    if strengths:
        st.success("Kết quả tốt ở " + ", ".join(strengths) + ". Hãy tiếp tục duy trì và vận dụng vào tình huống tư vấn.")
    if weak:
        st.warning("Cần ưu tiên củng cố " + ", ".join(weak) + ".")
    missing = []
    incorrect = []
    for q, a in fresh:
        try:
            fb = json.loads(a.get("Đánh giá từ Agent") or "{}")
        except (ValueError, TypeError):
            fb = {}
        for x in fb.get("missing_points", []):
            if x and x not in missing:
                missing.append(x)
        for x in fb.get("incorrect_points", []):
            if x and x not in incorrect:
                incorrect.append(x)
    if missing or incorrect:
        st.markdown("**Các nội dung cần ôn tập (theo kết quả chấm):**")
        for x in (missing + incorrect)[:8]:
            st.write("•", x)
    else:
        st.write("Chưa ghi nhận ý thiếu hoặc sai cụ thể từ phần chấm tự động. Hãy xem lại các câu có điểm thấp.")
    st.markdown("**3 lời khuyên:**")
    st.write("1. Ôn lại những câu có điểm dưới 50%, tập trung vào các ý còn thiếu đã được chỉ ra.")
    st.write("2. Tự giải thích lại kiến thức bằng ngôn ngữ của mình, sau đó đối chiếu tài liệu đào tạo.")
    st.write("3. Luyện vận dụng kiến thức vào tình huống tư vấn khách hàng trước bài đánh giá tiếp theo.")
    st.caption("Nhận xét dựa trên điểm và dữ liệu chấm đã lưu; không dùng tiêu chí ngoài đáp án tham khảo.")

def show_feedback(q, result):
    try:
        feedback = json.loads(result.get("Đánh giá từ Agent") or "{}")
    except (ValueError, TypeError):
        feedback = {}
    maximum = float(result.get("Điểm tối đa") or 1)
    score = float(result.get("Điểm đạt") or 0)
    if level(q) == 1:
        panel("Kết quả trắc nghiệm", "ĐÚNG" if score == maximum else "SAI", "feedback")
        correct_keys = q.get("_keys", [])
        correct_answer = "\n".join(f"{k}. {q.get('_choices', {}).get(k, '')}" for k in correct_keys)
        panel("Đáp án đúng", correct_answer, "reference")
        return
    st.write(f"**Điểm: {score:g}/{maximum:g}**")
    if norm(result.get("Trạng thái kiểm duyệt")).lower().startswith("pending"):
        st.warning("Kết quả cần Trainer kiểm duyệt.")
    for title, field in [("Các ý đúng", "correct_points"), ("Các ý còn thiếu", "missing_points"), ("Các ý chưa chính xác", "incorrect_points")]:
        if feedback.get(field):
            panel(title, "\n".join("• " + str(x) for x in feedback[field]), "feedback")
    if feedback.get("feedback"):
        panel("Nhận xét của Agent", feedback["feedback"], "feedback")
    # Do not expose reference answer unless the learner opens it.
    with st.expander("▸ Xem đáp án gợi ý", expanded=False):
        panel("Đáp án tham khảo", norm(q.get("Đáp án tham khảo")) or "Chưa có đáp án tham khảo.", "reference")
        st.caption("Các cách diễn đạt tương đương vẫn được chấp nhận.")


# API calls below are limited to initial loading and final batch commit.
# Cache reads are shared across users. Do not invalidate global caches after every submission.
def retry_sheets(action, attempts=5):
    for i in range(attempts):
        try:
            return action()
        except gspread.exceptions.APIError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status not in (429, 500, 502, 503, 504) or i == attempts - 1:
                raise
            time.sleep(min(2 ** i + random.random(), 16))

def get_initial_data():
    return (retry_sheets(lambda: records("Học viên")),
            retry_sheets(question_bank),
            retry_sheets(lambda: records("Câu trả lời")),
            retry_sheets(lambda: records("Bài kiểm tra")))

def grade_submission(student, selected_day, order, bank, responses, sid):
    output = []
    for item in order:
        qid = item["id"]
        q = bank[qid]
        answer = responses.get(qid, "")
        maximum = POINTS[level(q)]
        if not answer.strip():
            score, fb, needs_review = 0, {"feedback": "Không trả lời.", "correct_points": [], "missing_points": ["Chưa trả lời câu hỏi"], "incorrect_points": []}, False
        elif level(q) == 1:
            selected = sorted([x for x in answer.split(",") if x])
            correct = selected == sorted(q["_keys"])
            score = maximum if correct else 0
            fb = {"feedback": "Chính xác." if correct else "Chưa chính xác.", "correct_points": ["Đúng đáp án"] if correct else [], "missing_points": [] if correct else ["Cần ôn tập câu hỏi"], "incorrect_points": []}
            needs_review = False
        else:
            score, fb = grade_open(q, answer, maximum)
            needs_review = bool(fb.get("needs_review", False))
        output.append({"Session ID": sid, "Họ và tên": student, "Ngày": selected_day,
                       "Question ID": qid, "Ngày nội dung": q.get("Ngày nội dung"), "Module": q.get("Module"),
                       "Level": level(q), "Câu hỏi": q.get("Câu hỏi"), "Câu trả lời": answer,
                       "Đáp án tham khảo": q.get("Đáp án tham khảo") or q.get("Đáp án đề gốc"),
                       "Đánh giá từ Agent": json.dumps(fb, ensure_ascii=False), "Điểm đạt": score,
                       "Điểm tối đa": maximum, "Kết quả": "Cần Trainer kiểm duyệt" if needs_review else ("Đạt" if score/maximum >= 0.5 else "Chưa đạt"),
                       "Nội dung cần ôn": "; ".join(fb.get("missing_points", [])),
                       "Trang nguồn": q.get("Trang nguồn") or q.get("File nguồn"),
                       "Thời gian": timestamp(), "Trạng thái kiểm duyệt": "Pending Trainer review" if needs_review else "Auto graded",
                       "review": item.get("review", False)})
    return output

def commit_results(sid, student, selected_day, order, results, students):
    # Idempotency check prevents duplicate answer rows after retry/reconnect.
    # For strict concurrent idempotency use a transactional database.
    ws = sheet("Câu trả lời")
    existing = retry_sheets(lambda: ws.get_all_values())
    headers = existing[0] if existing else []
    if not headers:
        raise RuntimeError("Tab Câu trả lời thiếu hàng tiêu đề")
    if "Session ID" not in headers:
        raise RuntimeError("Tab Câu trả lời thiếu cột Session ID")
    sid_col = headers.index("Session ID")
    already = any(len(row) > sid_col and row[sid_col] == sid for row in existing[1:])
    if not already:
        # Use sheet header mapping, preserving existing column layout.
        data = [[r.get(h, "") for h in headers] for r in results]
        retry_sheets(lambda: ws.append_rows(data, value_input_option="RAW"))
    fresh_score = sum(float(r["Điểm đạt"]) for r in results if not r["review"])
    aws = sheet("Bài kiểm tra")
    assignment_rows = retry_sheets(lambda: aws.get_all_values())
    idx = next((i for i, row in enumerate(assignment_rows[1:], 2) if row and row[0] == sid), None)
    if idx is None:
        # Single append of assignment metadata after grading.
        retry_sheets(lambda: aws.append_row([sid, student, selected_day, "Hoàn thành", timestamp(), timestamp(), json.dumps(order, ensure_ascii=False), 13, len(order)-13, fresh_score, 32, timestamp()], value_input_option="RAW"))
    else:
        cols = {name: i+1 for i, name in enumerate(ASSIGN_HEADERS)}
        updates = [{"range": gspread.utils.rowcol_to_a1(idx, cols[k]), "values": [[v]]} for k, v in {"Trạng thái":"Hoàn thành", "Kết thúc":timestamp(), "Điểm mới":fresh_score, "Cập nhật lúc":timestamp()}.items()]
        retry_sheets(lambda: aws.batch_update(updates, value_input_option="RAW"))
    sws = sheet("Lượt đánh giá")
    summary = retry_sheets(lambda: sws.get_all_values())
    if not any(row and row[0] == sid for row in summary[1:]):
        dealer = next((x.get("Đại lý", "") for x in students if norm(x.get("Họ và tên")) == student), "")
        retry_sheets(lambda: sws.append_row([sid, student, dealer, selected_day, 1, timestamp(), timestamp(), len(order), fresh_score, 32, round(fresh_score / 32 * 100, 2), "", "", "", "", "13 câu mới; câu ôn tập không tính điểm", "", "Completed"], value_input_option="RAW"))
    return fresh_score

# Avoid loading data on every widget change: the full assessment uses one st.form.
try:
    students, bank, history, assignments = get_initial_data()
except Exception as exc:
    st.error("Không thể tải dữ liệu Google Sheets. Vui lòng thử lại sau.")
    st.exception(exc)
    st.stop()

with st.sidebar:
    mode = st.radio("Chế độ", ["Học viên", "Trainer – Xem trước", "Trainer – Cảnh báo"])
    selected_day = st.selectbox("Ngày đào tạo", [1, 2, 3, 4], format_func=lambda d: f"Ngày {d}")
    st.caption("13 câu mới/ngày · 32 điểm; câu ôn tập không cộng điểm")

names = [norm(x.get("Họ và tên")) for x in students if norm(x.get("Họ và tên"))]
if not names:
    st.warning("Chưa có học viên trong tab Học viên")
    st.stop()
student = st.selectbox("Học viên", names)

if mode.startswith("Trainer"):
    password = st.secrets.get("TRAINER_PASSWORD")
    if not password:
        st.error("Cần đặt TRAINER_PASSWORD trong Secrets")
        st.stop()
    if st.text_input("Mật khẩu Trainer", type="password") != password:
        st.stop()
    if mode == "Trainer – Cảnh báo":
        warnings = trainer_warnings(student, history)
        st.metric("Câu có ít nhất 2 lần không đạt", len(warnings))
        for qid, attempts in warnings.items():
            st.warning(f"{qid}: {len(attempts)} lần dưới 50%")
            st.write(bank.get(qid, {}).get("Câu hỏi", ""))
        st.stop()
    for lev, count in LEVEL_COUNTS.items():
        eligible = [q for q in bank.values() if 1 <= day(q) <= selected_day and level(q) == lev and approved(q) and (lev == 1 or norm(q.get("Đáp án tham khảo")))]
        st.write(f"Level {lev}: {len(eligible)} câu hợp lệ / cần {count}")
    st.stop()

st.subheader(f"Bài đánh giá – Ngày {selected_day}")
key = f"exam_v6:{student}:{selected_day}"
if key not in st.session_state:
    existing = assignment_row(student, selected_day, assignments)
    if existing and norm(existing.get("Trạng thái")) == "Hoàn thành":
        sid = norm(existing.get("Session ID"))
        order = json.loads(existing.get("Danh sách câu JSON") or "[]")
        completed = {norm(x.get("Question ID")): x for x in history if norm(x.get("Session ID")) == sid}
        st.success("Bài kiểm tra đã hoàn thành và lưu kết quả.")
        if completed:
            show_final_report(order, completed, bank)
            for i, item in enumerate(order, 1):
                if item["id"] in completed and item["id"] in bank:
                    with st.expander(f"Câu {i} – Level {level(bank[item['id']])}"):
                        st.write(bank[item["id"]].get("Câu hỏi", ""))
                        show_feedback(bank[item["id"]], completed[item["id"]])
        st.stop()
    proposed, shortage = build_assignment(student, selected_day, bank, history)
    if shortage:
        st.error("Không đủ câu hỏi hợp lệ để tạo bài kiểm tra")
        for lev, (actual, required) in shortage.items():
            st.write(f"Level {lev}: {actual}/{required}")
        st.stop()
    if st.button("Bắt đầu bài kiểm tra", type="primary"):
        st.session_state[key] = {"sid": str(uuid.uuid4()), "order": proposed, "status": "taking", "results": None, "responses": None}
        st.rerun()
    st.stop()

exam = st.session_state[key]
order = exam["order"]
if exam["status"] == "taking":
    st.info(f"{len(order)} câu hỏi: 13 câu mới + {len(order)-13} câu ôn tập. Chỉ nộp bài một lần.")
    with st.form(f"assessment:{key}"):
        responses = {}
        for i, item in enumerate(order, 1):
            q = bank.get(item["id"])
            if not q:
                st.error(f"Thiếu câu hỏi {item['id']} trong ngân hàng")
                st.stop()
            lev = level(q)
            st.markdown(f"### Câu {i}/{len(order)} · Level {lev} · {'Ôn tập' if item['review'] else str(POINTS[lev])+' điểm'}")
            panel("Câu hỏi", q.get("Câu hỏi", ""), "question")
            if lev == 1:
                # Shuffle option order without changing the underlying answer keys.
                keys = sorted(q["_choices"])
                seed = int(hashlib.sha256(f"{exam['sid']}|{item['id']}".encode()).hexdigest(), 16)
                random.Random(seed).shuffle(keys)
                if len(q["_keys"]) > 1:
                    selected = [k for k in keys if st.checkbox(f"{k}. {q['_choices'][k]}", key=f"{key}:{item['id']}:{k}")]
                else:
                    selected = st.radio("Chọn đáp án", keys, index=None, key=f"{key}:{item['id']}", format_func=lambda k: f"{k}. {q['_choices'][k]}")
                    selected = [selected] if selected else []
                responses[item["id"]] = ",".join(sorted(selected))
            else:
                responses[item["id"]] = st.text_area("Câu trả lời", key=f"{key}:{item['id']}", height=120)
        submitted = st.form_submit_button("Nộp bài kiểm tra", type="primary")
    if submitted:
        if any(not responses.get(item["id"], "").strip() for item in order):
            st.warning("Bạn còn câu chưa trả lời. Vui lòng hoàn thành trước khi nộp.")
            st.stop()
        exam["responses"] = responses
        exam["status"] = "grading"
        st.rerun()
    st.stop()

if exam["status"] == "grading":
    with st.spinner("Đang chấm điểm và tạo nhận xét. Vui lòng không tải lại trang..."):
        try:
            exam["results"] = grade_submission(student, selected_day, order, bank, exam["responses"], exam["sid"])
            exam["status"] = "saving"
            st.rerun()
        except Exception as exc:
            st.error(f"Chấm điểm chưa hoàn tất: {exc}")
            st.warning("Chưa ghi kết quả lên Google Sheets. Bấm thử lại để chấm lại; không đóng trang.")
            if st.button("Thử chấm lại"):
                st.rerun()
    st.stop()

# Automatically persist the entire graded exam, without a second learner action.
if exam["status"] == "saving":
    with st.spinner("Đang tự động lưu kết quả lên Google Sheets..."):
        try:
            commit_results(exam["sid"], student, selected_day, order, exam["results"], students)
            exam["status"] = "saved"
            st.rerun()
        except Exception as exc:
            exam["status"] = "save_failed"
            exam["save_error"] = str(exc)
            st.rerun()
    st.stop()

results = exam["results"] or []
completed = {r["Question ID"]: r for r in results}
if exam["status"] == "saved":
    st.success("Đã chấm bài và tự động lưu kết quả lên Google Sheets thành công.")
else:
    st.error("Đã chấm bài nhưng Google Sheets chưa xác nhận lưu thành công. Kết quả vẫn được giữ trong phiên hiện tại.")
    st.caption(exam.get("save_error", "Lỗi đồng bộ Google Sheets."))
    if st.button("Thử đồng bộ lại kết quả", type="primary"):
        exam["status"] = "saving"
        st.rerun()
show_final_report(order, completed, bank)
for i, item in enumerate(order, 1):
    q = bank[item["id"]]
    with st.expander(f"Câu {i} – Level {level(q)} · {completed[item['id']]['Điểm đạt']}/{POINTS[level(q)]} điểm"):
        panel("Câu hỏi", q.get("Câu hỏi", ""), "question")
        panel("Câu trả lời của học viên", completed[item["id"]].get("Câu trả lời", ""), "answer")
        show_feedback(q, completed[item["id"]])
