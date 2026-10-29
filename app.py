"""FA Knowledge Coach v2 – daily 13 new questions + accumulated remediation."""
import hashlib
import json
import random
import re
import uuid
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
    return norm(row.get("Trạng thái duyệt")).casefold() in APPROVED

@st.cache_resource(show_spinner=False)
def book():
    creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID)

def sheet(name):
    return book().worksheet(name)

def records(name):
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

def answers_for(student):
    return [x for x in records("Câu trả lời") if norm(x.get("Họ và tên")) == student and norm(x.get("Question ID"))]

def prior_failures(student, selected_day, bank):
    # Most recent graded result per question governs the outstanding remediation queue.
    history = answers_for(student)
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

def build_assignment(student, selected_day, bank):
    fresh = []
    missing = {}
    for lev, required in LEVEL_COUNTS.items():
        eligible = [qid for qid, q in bank.items() if approved(q) and day(q) == selected_day and level(q) == lev]
        if len(eligible) < required:
            missing[lev] = (len(eligible), required)
        else:
            fresh += stable_sample(eligible, required, student, selected_day, lev)
    if missing:
        return None, missing
    repeat = [qid for qid in prior_failures(student, selected_day, bank) if qid not in fresh]
    order = [{"id": qid, "review": False} for qid in fresh] + [{"id": qid, "review": True} for qid in repeat]
    seed = int(hashlib.sha256(f"{student}|{selected_day}|order".encode()).hexdigest(), 16)
    random.Random(seed).shuffle(order)
    return order, {}

def assignment_row(student, selected_day):
    ws = ensure_tab("Bài kiểm tra", ASSIGN_HEADERS)
    for row in ws.get_all_records(default_blank=""):
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
                "Bạn là giám khảo Ford Academy. Chỉ dựa trên đáp án tham khảo và rubric đã duyệt. "
                "Đánh giá tính chính xác và độ bao phủ các ý chính; không tự bổ sung dữ kiện kỹ thuật. "
                "Trả JSON: ratio (0..1), correct_points (array strings), missing_points (array strings), "
                "incorrect_points (array strings), feedback (string), needs_review (boolean). "
                "Nếu nguồn không đủ hoặc mơ hồ, needs_review=true."
            )},
            {"role": "user", "content": json.dumps({
                "question": question.get("Câu hỏi"), "reference": question.get("Đáp án tham khảo"),
                "rubric": question.get("Rubric"), "answer": answer
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

def trainer_warnings(student):
    failed = {}
    for x in answers_for(student):
        try:
            score, maximum = float(x.get("Điểm đạt")), float(x.get("Điểm tối đa"))
            if maximum <= 0 or norm(x.get("Trạng thái kiểm duyệt")).lower().startswith("pending"):
                continue
        except (TypeError, ValueError):
            continue
        if score / maximum < 0.5:
            failed.setdefault(norm(x.get("Question ID")), []).append(x)
    return {qid: attempts for qid, attempts in failed.items() if len(attempts) >= 2}

def show_feedback(q, result):
    try:
        feedback = json.loads(result.get("Đánh giá từ Agent") or "{}")
    except (ValueError, TypeError):
        feedback = {}
    maximum = float(result.get("Điểm tối đa") or 1)
    score = float(result.get("Điểm đạt") or 0)
    st.metric("Kết quả", f"{score:g}/{maximum:g}")
    if norm(result.get("Trạng thái kiểm duyệt")).lower().startswith("pending"):
        st.warning("Chờ Trainer kiểm duyệt; chưa tính vào cảnh báo.")
    else:
        (st.success if score / maximum >= 0.5 else st.warning)("Đạt ngưỡng 50%" if score / maximum >= 0.5 else "Chưa đạt 50% – sẽ được ôn tập")
    for title, field in [("Các ý đúng", "correct_points"), ("Các ý còn thiếu", "missing_points"), ("Các ý chưa chính xác", "incorrect_points")]:
        if feedback.get(field):
            st.markdown(f"**{title}:**")
            for item in feedback[field]:
                st.write("•", item)
    if feedback.get("feedback"):
        st.info(feedback["feedback"])
    # Never display the answer key during a live student assessment.

try:
    students = records("Học viên")
    bank = question_bank()
except Exception as exc:
    st.error("Không đọc được Google Sheets. Kiểm tra Secrets và quyền truy cập.")
    st.exception(exc)
    st.stop()

with st.sidebar:
    mode = st.radio("Chế độ", ["Học viên", "Trainer – Xem trước", "Trainer – Cảnh báo"])
    selected_day = st.selectbox("Ngày đào tạo", [1, 2, 3, 4], format_func=lambda d: f"Ngày {d}")
    st.caption("13 câu mới/ngày · 32 điểm tối đa. Câu ôn tập không cộng điểm.")

names = [norm(x.get("Họ và tên")) for x in students if norm(x.get("Họ và tên"))]
if not names:
    st.warning("Chưa có học viên trong tab Học viên")
    st.stop()
student = st.selectbox("Học viên", names)

if mode.startswith("Trainer"):
    # A separate password prevents accidental disclosure in the pilot; deploy behind SSO for production.
    password = st.secrets.get("TRAINER_PASSWORD")
    if not password:
        st.error("Cần đặt TRAINER_PASSWORD trong Streamlit Secrets để sử dụng chế độ Trainer.")
        st.stop()
    entered = st.text_input("Mật khẩu Trainer", type="password")
    if entered != password:
        st.stop()
    if mode == "Trainer – Cảnh báo":
        warnings = trainer_warnings(student)
        st.metric("Câu có ít nhất 2 lần không đạt", len(warnings))
        for qid, attempts in warnings.items():
            st.warning(f"{qid} · {len(attempts)} lần dưới 50%")
            st.write(bank.get(qid, {}).get("Câu hỏi", ""))
            st.caption("Điểm các lần: " + " → ".join(f"{float(a['Điểm đạt'])/float(a['Điểm tối đa']):.0%}" for a in attempts))
        if not warnings:
            st.success("Không có cảnh báo cho học viên này")
        st.stop()
    st.subheader(f"Xem trước – Ngày {selected_day}")
    for lev, count in LEVEL_COUNTS.items():
        available = [q for q in bank.values() if day(q) == selected_day and level(q) == lev]
        st.write(f"**Level {lev}: {len([q for q in available if approved(q)])}/{count} câu đã duyệt cần thiết**")
        for q in available:
            with st.expander(f"{q['Question ID']} · {q['Câu hỏi'][:90]} {'✓' if approved(q) else '(Chờ duyệt)'}"):
                st.write(q["Câu hỏi"])
                if lev == 1:
                    st.write(q.get("_choices", {}))
                    st.write("Đáp án:", ", ".join(q.get("_keys", [])))
                else:
                    st.write("Đáp án tham khảo:", q.get("Đáp án tham khảo", ""))
                    st.write("Rubric:", q.get("Rubric", ""))
    st.stop()

st.subheader(f"Bài đánh giá – Ngày {selected_day}")
existing = assignment_row(student, selected_day)
if not existing:
    proposed, shortage = build_assignment(student, selected_day, bank)
    if shortage:
        st.error("Chưa đủ câu hỏi đã duyệt để tạo bài kiểm tra đúng cấu trúc.")
        for lev, (actual, required) in shortage.items():
            st.write(f"Level {lev}: có {actual}/{required} câu Approved")
        st.caption("Trainer có thể xem trước câu chưa duyệt trong chế độ Trainer.")
        st.stop()
    st.write(f"13 câu mới + {len(proposed)-13} câu ôn tập · Điểm mới tối đa 32")
    if st.button("Bắt đầu bài kiểm tra", type="primary"):
        try:
            start_assignment(student, selected_day, proposed)
            st.rerun()
        except Exception as exc:
            st.error(f"Không lưu được bài kiểm tra: {exc}")
    st.stop()

sid = norm(existing["Session ID"])
order = json.loads(existing["Danh sách câu JSON"])
completed = previous_results(sid)
if norm(existing.get("Trạng thái")) == "Hoàn thành":
    st.success("Bạn đã hoàn thành bài kiểm tra! Xin cảm ơn bạn đã tham gia đánh giá kiến thức Ford Academy.")
    st.stop()

next_item = next((item for item in order if item["id"] not in completed), None)
if next_item is None:
    fresh_score = sum(float(completed[item["id"]].get("Điểm đạt") or 0) for item in order if not item["review"])
    try:
        update_assignment(sid, **{"Trạng thái": "Hoàn thành", "Kết thúc": timestamp(), "Điểm mới": fresh_score, "Cập nhật lúc": timestamp()})
        # Append one summary record, preserving the existing 18-column schema.
        sheet("Lượt đánh giá").append_row([
            sid, student, next((x.get("Đại lý", "") for x in students if norm(x.get("Họ và tên")) == student), ""),
            selected_day, 1, existing.get("Bắt đầu", ""), timestamp(), len(order), fresh_score, 32,
            round(fresh_score / 32 * 100, 2), "", "", "", "", "13 câu mới; câu ôn tập không tính điểm", "", "Completed"
        ], value_input_option="RAW")
        st.rerun()
    except Exception as exc:
        st.error(f"Chưa chốt được bài kiểm tra: {exc}")
    st.stop()

index = next(i for i, item in enumerate(order) if item["id"] == next_item["id"])
q = bank.get(next_item["id"])
if not q:
    st.error("Câu hỏi đã bị xóa khỏi Question Bank. Trainer cần khôi phục câu trước khi tiếp tục.")
    st.stop()
st.progress(len(completed) / len(order))
st.write(f"**Câu {index+1}/{len(order)}** · Level {level(q)} · {'Ôn tập' if next_item['review'] else str(POINTS[level(q)])+' điểm'}")
st.write(q.get("Câu hỏi", ""))

# Submitted answer remains locked while feedback is shown; resuming after refresh reads Google Sheets.
feedback_key = f"feedback:{sid}:{next_item['id']}"
if feedback_key in st.session_state:
    show_feedback(q, st.session_state[feedback_key])
    if st.button("Câu tiếp theo", type="primary"):
        del st.session_state[feedback_key]
        st.rerun()
    st.stop()

with st.form(f"form:{sid}:{next_item['id']}"):
    if level(q) == 1:
        keys = list(q["_choices"])
        if len(q["_keys"]) > 1:
            st.caption("Chọn tất cả phương án đúng")
            picks = [k for k in keys if st.checkbox(f"{k}. {q['_choices'][k]}")]
        else:
            picked = st.radio("Chọn đáp án", keys, index=None, format_func=lambda k: f"{k}. {q['_choices'][k]}")
            picks = [picked] if picked else []
        answer = ",".join(sorted(picks))
    else:
        answer = st.text_area("Câu trả lời", height=160)
    sent = st.form_submit_button("Gửi đáp án", type="primary")
if sent:
    if not answer.strip():
        st.warning("Hãy nhập hoặc chọn đáp án trước khi gửi.")
        st.stop()
    maximum = POINTS[level(q)]
    try:
        if level(q) == 1:
            correct = sorted(picks) == q["_keys"]
            score = maximum if correct else 0
            feedback = {"feedback": "Chính xác." if correct else "Chưa chính xác; câu hỏi sẽ xuất hiện trong phần ôn tập.",
                        "correct_points": ["Đã chọn đúng phương án."] if correct else [],
                        "missing_points": [] if correct else ["Cần ôn tập nội dung câu hỏi."], "incorrect_points": []}
            needs_review = False
        else:
            score, feedback = grade_open(q, answer, maximum)
            needs_review = bool(feedback.get("needs_review", False))
        save_answer(sid, student, selected_day, q, answer, score, maximum, feedback, needs_review)
        st.session_state[feedback_key] = {
            "Điểm đạt": score, "Điểm tối đa": maximum,
            "Đánh giá từ Agent": json.dumps(feedback, ensure_ascii=False),
            "Trạng thái kiểm duyệt": "Pending Trainer review" if needs_review else "Auto graded",
        }
        st.rerun()
    except Exception as exc:
        st.error(f"Chưa chấm hoặc lưu được câu trả lời. Có thể thử gửi lại: {exc}")
