"""FA Knowledge Coach v6 – single submission, batch grading and Sheets writes."""
import hashlib
import smtplib
from email.message import EmailMessage
import json
import random
import re
import uuid
import time
import threading
from collections import deque
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
PASS_RATIO = 0.60
VECTOR_STORE_ID = str(st.secrets.get("OPENAI_VECTOR_STORE_ID", "vs_6aa3c4ac78b08191b4b4e8e3bf6ecf33")).strip()

def is_passed(score, maximum):
    try:
        maximum = float(maximum)
        return maximum > 0 and float(score) / maximum >= PASS_RATIO
    except (TypeError, ValueError):
        return False

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

class SheetsRequestBudget:
    """One rolling-window budget shared by all sessions in this app process."""
    def __init__(self, clock=None, sleeper=None, limit=48, window=60.5):
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.limit, self.window = limit, window
        self.events = {"read": deque(), "write": deque()}
        self.cooldown = {"read": 0.0, "write": 0.0}
        self.lock = threading.Lock()

    def acquire(self, kind):
        while True:
            with self.lock:
                now = self.clock()
                events = self.events[kind]
                while events and events[0] <= now - self.window:
                    events.popleft()
                wait = max(0.0, self.cooldown[kind] - now)
                if len(events) >= self.limit:
                    wait = max(wait, events[0] + self.window - now)
                if wait <= 0:
                    events.append(now)
                    return
            self.sleeper(min(wait + .01, 1.0))

    def exhausted(self, kind):
        with self.lock:
            self.cooldown[kind] = max(self.cooldown[kind], self.clock() + 61.0)


@st.cache_resource(show_spinner=False)
def sheets_budget():
    return SheetsRequestBudget()


class BudgetedSheetsHTTPClient(gspread.HTTPClient):
    def request(self, method, endpoint, *args, **kwargs):
        # Covers metadata, reads, writes and calls not wrapped in retry_sheets.
        kind = "read" if str(method).lower() in ("get", "head") else "write"
        for attempt in range(3):
            sheets_budget().acquire(kind)
            try:
                return super().request(method, endpoint, *args, **kwargs)
            except gspread.exceptions.APIError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status != 429:
                    raise
                sheets_budget().exhausted(kind)
                if attempt == 2:
                    raise
                # Only an explicit rejected quota request is retried here.
                # Never retry uncertain append/network failures at transport level.


@st.cache_resource(show_spinner=False)
def book():
    creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(creds, http_client=BudgetedSheetsHTTPClient)
    client.set_timeout((10, 45))
    return client.open_by_key(SHEET_ID)


@st.cache_resource(ttl=300, show_spinner=False)
def worksheet_index():
    return {ws.title: ws for ws in book().worksheets()}


def sheet(name):
    try:
        return worksheet_index()[name]
    except KeyError:
        worksheet_index.clear()
        try:
            return worksheet_index()[name]
        except KeyError:
            raise gspread.WorksheetNotFound(name)


@st.cache_data(ttl=300, show_spinner=False)
def records(name):
    """Read table rows without treating unused trailing grid columns as headers."""
    values = sheet(name).get_all_values(value_render_option="UNFORMATTED_VALUE")
    if not values:
        return []
    headers = [str(value or "").strip() for value in values[0]]
    while headers and not headers[-1]:
        headers.pop()
    if not headers or any(not header for header in headers):
        raise RuntimeError(f"Tab {name} có ô trống trong hàng tiêu đề; kiểm tra hàng 1.")
    if len(set(headers)) != len(headers):
        raise RuntimeError(f"Tab {name} có tiêu đề cột trùng nhau; kiểm tra hàng 1.")
    result = []
    for raw in values[1:]:
        row = list(raw[:len(headers)]) + [""] * max(0, len(headers) - len(raw))
        if any(str(value or "").strip() for value in row):
            result.append(dict(zip(headers, row[:len(headers)])))
    return result


def invalidate_records(*names):
    # Keep static roster/bank caches warm when a learner saves or Trainer grades.
    for name in names or ("Câu trả lời", "Bài kiểm tra", "Lượt đánh giá"):
        records.clear(name)


def ensure_tab(name, headers):
    try:
        return sheet(name)
    except gspread.WorksheetNotFound:
        ws = book().add_worksheet(title=name, rows=2000, cols=max(20, len(headers)))
        ws.append_row(headers, value_input_option="RAW")
        worksheet_index()[name] = ws
        return ws


def question_bank():
    result = {}
    for row in records("Question Bank - L2/3/4"):
        qid = norm(row.get("Question ID"))
        if qid and level(row) in (2, 3, 4):
            item = dict(row)
            # The live Sheet uses the longer column title; normalize it for the grader.
            item["Đáp án tham khảo"] = norm(
                row.get("Đáp án tham khảo") or row.get("Đáp án tham khảo, ý chính")
            )
            result[qid] = item
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
            earned = float(effective_score(x))
            maximum = float(x.get("Điểm tối đa"))
            if maximum <= 0:
                continue
        except (TypeError, ValueError):
            continue
        latest[qid] = earned / maximum
    return sorted(qid for qid, ratio in latest.items() if not is_passed(ratio, 1.0))

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
            updates = [{"range": gspread.utils.rowcol_to_a1(index, ASSIGN_HEADERS.index(key)+1), "values": [[value]]}
                       for key, value in values.items()]
            retry_sheets(lambda: ws.batch_update(updates, value_input_option="RAW"))
            return
    raise RuntimeError("Không tìm thấy bài kiểm tra để cập nhật")

def correct_points_feedback(points):
    """Store concise, supported correct ideas only in the feedback column."""
    if not isinstance(points, list):
        points = []
    cleaned = []
    for point in points:
        if not isinstance(point, str):
            continue
        text = point.strip().lstrip("•- ").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return "\n".join("• " + point for point in cleaned) or "Chưa xác định được ý đúng phù hợp với đáp án hướng dẫn."


def should_use_vector_store(question):
    """Enable Ford product retrieval only when the question's Rubric requests it."""
    rubric = norm(question.get("Rubric"))
    if not rubric:
        return False
    compact = " ".join(rubric.casefold().split())
    negative = any(phrase in compact for phrase in ("không dùng", "không tra", "không sử dụng"))
    markers = (
        "ford_comparison_data.json",
        "ford_comparison",
        "vector store",
        "vector_store",
        "[nguồn: ford comparison",
        "[nguồn đối chiếu]",
    )
    return any(marker in compact for marker in markers) and not negative


def vector_store_context(client, question, answer):
    if not should_use_vector_store(question):
        return ""
    if not VECTOR_STORE_ID:
        raise RuntimeError("Chưa cấu hình OPENAI_VECTOR_STORE_ID cho câu hỏi yêu cầu tra cứu Ford Product data")
    rubric = norm(question.get("Rubric"))
    query = (
        "Question: " + norm(question.get("Câu hỏi")) + chr(10)
        + "Learner answer: " + norm(answer) + chr(10)
        + "Rubric: " + rubric
    )
    page = client.vector_stores.search(
        vector_store_id=VECTOR_STORE_ID,
        query=query,
        max_num_results=8,
    )
    chunks = []
    for item in (getattr(page, "data", None) or []):
        if isinstance(item, dict):
            filename = item.get("filename", "")
            score = item.get("score", "")
            parts = item.get("content", []) or []
        else:
            filename = getattr(item, "filename", "")
            score = getattr(item, "score", "")
            parts = getattr(item, "content", []) or []
        texts = []
        for part in parts:
            text = part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
            if text:
                texts.append(str(text))
        if texts:
            chunks.append(
                f"[Nguồn: {filename}; score={score}]" + chr(10) + chr(10).join(texts)
            )
    return chr(10) + chr(10).join(chunks) or "VECTOR_STORE_NO_MATCH"


def grade_open(question, answer, max_points):
    key = st.secrets.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("Chưa có OPENAI_API_KEY trong Streamlit Secrets")
    if not norm(question.get("Đáp án tham khảo")):
        raise RuntimeError("Câu hỏi thiếu đáp án tham khảo; Trainer cần bổ sung")
    client = OpenAI(api_key=key)
    knowledge_context = vector_store_context(client, question, answer)
    completion = client.chat.completions.create(
        model=st.secrets.get("OPENAI_MODEL", "gpt-4.1-mini"),
        temperature=0, response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Bạn là giám khảo Ford Academy. Căn cứ chấm gồm câu hỏi, đáp án tham khảo đã được Trainer duyệt "
                "và trainer_rules là phiên bản mới nhất của tab Quy tắc đánh giá trong Google Sheet. "
                "knowledge_context là các đoạn dữ liệu được truy xuất từ Ford Product Vector Store; chỉ dùng nó khi Rubric yêu cầu, "
                "Nếu trường Rubric riêng của câu hỏi có nội dung, bắt buộc đọc và áp dụng; nếu Rubric trống thì bỏ qua Rubric và không tự tạo tiêu chí từ Rubric. "
                "Tuân thủ trainer_rules: hiểu ý theo ngữ cảnh; chấp nhận từ đồng nghĩa, viết tắt, lỗi chính tả nhỏ "
                "và cách diễn đạt tương đương; không thêm kiến thức học viên chưa thể hiện; không suy đoán khi câu trả lời mơ hồ; "
                "chấm theo từng ý hoặc mệnh đề độc lập; không dùng nhãn “đúng một phần”; phải tách riêng phần đúng và phần sai, không tính trùng một ý nhiều lần. "
                "Không chấm theo keyword cứng và không yêu cầu học viên lặp nguyên văn. Nếu Rubric của câu Đèn Matrix xác định đúng là 9 chip LED thì câu trả lời nói 12 chip LED phải ghi vào “Ý sai/không phù hợp”, không được ghi là ý hay hoặc cộng điểm; nếu cùng câu có mô tả đúng cơ chế thì tách riêng và vẫn ghi nhận phần đúng. Nếu Rubric không nêu thông số thì không tự suy đoán. Với mọi tính năng hỗ trợ, đặc biệt hỗ trợ lái, phải phân biệt hỗ trợ với tự động hoàn toàn hoặc hands-free; không suy rộng khả năng, và phát biểu vượt quá điều kiện vận hành được nguồn xác nhận là sai rõ ràng. Giá trị “Chưa có dữ liệu”, “-”, null hoặc ô trống nghĩa là chưa đủ căn cứ; không được hiểu là “Không có”, không suy đoán và không cộng/trừ điểm. "
                "Agent phải tự chấm trọn gói, gồm ý đúng, ý thiếu, ý sai rõ ràng và ý ngoài đáp án; không yêu cầu bước phân loại thủ công. "
                "ratio là điểm cuối trong khoảng 0..1, không vượt điểm tối đa. Ý ngoài đáp án đúng và liên quan có thể được tính trong điểm cuối "
                "khi Rubric hoặc trainer_rules cho phép và có căn cứ rõ ràng; ý ngoài đáp án mơ hồ thì không tính điểm; ý trái Rubric hoặc thông số được Rubric xác định là sai rõ ràng, không được chuyển thành ý hay hoặc điểm cộng. "
                "Không tự thêm kiến thức hoặc tự suy đoán. Nếu không chắc chắn, needs_review=true. "
                "Trả JSON: ratio (0..1), correct_points (array strings), outside_correct_points (array strings), "
                "outside_wrong_points (array strings), outside_uncertain_points (array strings), missing_points (array strings), "
                "incorrect_points (array strings), feedback (string), needs_review (boolean). "
                "Mỗi ý là một phần tử riêng, giữ nguyên điều kiện và ngữ cảnh, không lặp ý. "
                "feedback phải ngắn gọn, phù hợp với màn hình điện thoại và chỉ hiển thị mục có nội dung, theo đúng thứ tự: Các ý đã trình bày đúng; Các ý có thể bổ sung; Ý hay được cộng thêm điểm; Ý sai/không phù hợp. Bỏ hẳn mục không có nội dung, không ghi “Không có”. Mỗi ý một dòng, không lan man. Trong mục Các ý có thể bổ sung, chỉ liệt kê các ý còn thiếu; sau danh sách chỉ thêm một lần câu “Bạn có thể bổ sung các ý trên để phần tư vấn thuyết phục hơn.”, không lặp câu này cho từng ý. Ý học viên đã nêu đúng đưa vào mục Các ý đã trình bày đúng; ý có trong đáp án/Rubric nhưng học viên chưa nêu đưa vào mục Các ý có thể bổ sung; không chuyển đổi hai mục. Không nhắc đến đáp án tham khảo, đáp án mẫu, Rubric, trainer_rules hoặc cách hệ thống đối chiếu trong feedback; hãy viết như nhận xét trực tiếp dành cho học viên."
            )},
            {"role": "user", "content": json.dumps({
                "question": question.get("Câu hỏi"), "reference": question.get("Đáp án tham khảo"),
                "rubric": question.get("Rubric", ""),
                "trainer_rules": question.get("_trainer_rules", ""),
                "knowledge_context": knowledge_context,
                "stage_rules": "Agent chấm trọn gói theo trainer_rules mới nhất. Nếu Rubric có nội dung thì bắt buộc áp dụng; nếu Rubric trống thì bỏ qua. Tách từng ý hoặc mệnh đề độc lập, không dùng nhãn đúng một phần; thông số trái Rubric là sai rõ ràng. Với câu Đèn Matrix, 9 chip LED là đúng theo Rubric và 12 chip LED là sai/không phù hợp, không cộng điểm. Chấp nhận diễn đạt tương đương, không suy đoán ý mơ hồ, không chấm keyword cứng và không yêu cầu bước phân loại thủ công. Điểm cuối không vượt điểm tối đa và ngưỡng Đạt là từ 60% trở lên. Giá trị “Chưa có dữ liệu”, “-”, null hoặc ô trống là chưa đủ căn cứ; không được hiểu là “Không có”, không suy đoán và không cộng/trừ điểm.",
                "answer": answer
            }, ensure_ascii=False)}
        ])
    data = json.loads(completion.choices[0].message.content)
    list_fields = (
        "correct_points", "outside_correct_points", "outside_wrong_points",
        "outside_uncertain_points", "missing_points", "incorrect_points"
    )
    for field in list_fields:
        values = data.get(field, [])
        if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
            raise RuntimeError(f"Agent trả {field} không hợp lệ; chưa lưu kết quả.")
        data[field] = unique_ideas(values)
    outside = []
    outside.extend("Đúng ngoài đáp án: " + x for x in data.get("outside_correct_points", []))
    outside.extend("Sai rõ ràng ngoài đáp án: " + x for x in data.get("outside_wrong_points", []))
    outside.extend("Chưa đủ căn cứ: " + x for x in data.get("outside_uncertain_points", []))
    data["outside_points"] = unique_ideas(outside)
    if not isinstance(data.get("feedback"), str):
        data["feedback"] = ""
    ratio = min(1.0, max(0.0, float(data.get("ratio", 0))))
    return round(ratio * max_points, 2), data

def save_answer(sid, student, selected_day, q, response, score, maximum, feedback, review):
    sheet("Câu trả lời").append_row([
        sid, student, selected_day, q.get("Question ID"), q.get("Ngày nội dung"),
        q.get("Module"), level(q), q.get("Câu hỏi"), response,
        q.get("Đáp án tham khảo") or q.get("Đáp án đề gốc"), json.dumps(feedback, ensure_ascii=False),
        score, maximum, "Cần Trainer kiểm duyệt" if review else ("Đạt" if is_passed(score, maximum) else "Chưa đạt"),
        "; ".join(feedback.get("missing_points", [])), q.get("Trang nguồn") or q.get("File nguồn"),
        timestamp(), "Pending Trainer review" if review else "Auto graded",
    ], value_input_option="RAW")

def previous_results(sid):
    return {norm(x.get("Question ID")): x for x in records("Câu trả lời") if norm(x.get("Session ID")) == sid}

def trainer_warnings(student, history=None):
    failed = {}
    for x in answers_for(student, history):
        try:
            score, maximum = float(effective_score(x)), float(x.get("Điểm tối đa"))
            if maximum <= 0 or norm(x.get("Trạng thái kiểm duyệt")).lower().startswith("pending"):
                continue
        except (TypeError, ValueError):
            continue
        if not is_passed(score, maximum):
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
        elif ratio < PASS_RATIO:
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
    st.write("1. Ôn lại những câu có điểm chưa đạt (điểm dưới 60%), tập trung vào các ý còn thiếu đã được chỉ ra.")
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
    # Learner-facing feedback uses the Trainer's comments and missing points only.


# API calls below are limited to initial loading and final batch commit.
# Cache reads are shared across users. Do not invalidate global caches after every submission.
def retry_sheets(action, attempts=5):
    for i in range(attempts):
        try:
            return action()
        except gspread.exceptions.APIError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status not in (500, 502, 503, 504) or i == attempts - 1:
                raise
            time.sleep(min(2 ** i + random.random(), 16))

def get_initial_data(role="Học viên"):
    # Trainer-only historical data is loaded only when required.
    students = retry_sheets(lambda: records("Học viên"))
    bank = retry_sheets(question_bank)
    history = retry_sheets(lambda: records("Câu trả lời")) if role == "Trainer" else []
    assignments = retry_sheets(lambda: records("Bài kiểm tra"))
    return students, bank, history, assignments

@st.cache_data(ttl=300, show_spinner=False)
def cached_course_values():
    return retry_sheets(lambda: sheet("Khóa học").get_all_values())

def append_data_rows(ws, rows):
    """Append after the sheet's last data row, without logical-table detection.

    Do not retry this non-idempotent operation blindly; callers re-read IDs.
    """
    ws.spreadsheet.batch_update({"requests": [{"appendCells": {
        "sheetId": ws.id,
        "rows": [{"values": [{"userEnteredValue": {"boolValue": v} if isinstance(v, bool) else {"stringValue": str(v if v is not None else "")}}
                              for v in row]} for row in rows],
        "fields": "userEnteredValue",
    }}]})


def save_raw_answers(sid, student, selected_day, order, bank, responses):
    """Persist and verify A:I before any AI call; never overwrite existing raw answers."""
    ws = sheet("Câu trả lời")
    # Read stored text, not locale-dependent display formatting.
    values = retry_sheets(lambda: ws.get_all_values(value_render_option="UNFORMATTED_VALUE"))
    headers = values[0] if values else []
    expected = ["Session ID", "Họ và tên", "Ngày đánh giá", "Question ID",
                "Ngày khóa học", "Module", "Level", "Câu hỏi", "Nội dung trả lời học viên"]
    if headers[:9] != expected:
        raise RuntimeError("Cấu trúc A:I của tab Câu trả lời đã thay đổi; dừng để bảo vệ dữ liệu.")
    existing = {}
    for idx, row in enumerate(values[1:], 2):
        if row and row[0] == sid and len(row) > 3:
            existing.setdefault(row[3], []).append((idx, row))
    ids = [item["id"] for item in order]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Bộ đề chứa Question ID trùng.")
    if any(len(existing.get(qid, [])) > 1 for qid in ids):
        raise RuntimeError("Có câu trả lời trùng trong Session ID; cần Trainer kiểm tra.")
    for item in order:
        qid = item["id"]
        if qid in existing:
            saved = existing[qid][0][1]
            if len(saved) < 9 or saved[8] != responses[qid]:
                raise RuntimeError(f"Câu {qid} đã lưu với nội dung khác; không ghi đè.")
    missing = [item for item in order if item["id"] not in existing]
    if missing:
        rows = []
        for item in missing:
            q = bank[item["id"]]
            rows.append([sid, student, str(selected_day), item["id"],
                         q.get("Ngày nội dung", ""), q.get("Module", ""),
                         level(q), q.get("Câu hỏi", ""), responses[item["id"]]])
        append_data_rows(ws, rows)
    # Read-after-write verification, including every original answer.
    check = retry_sheets(lambda: ws.get_all_values(value_render_option="UNFORMATTED_VALUE"))
    found = {}
    for row in check[1:]:
        if row and row[0] == sid and len(row) > 3:
            found.setdefault(row[3], []).append(row)
    if set(found) != set(ids) or any(len(found[qid]) != 1 or
       len(found[qid][0]) < 9 or found[qid][0][8] != responses[qid] for qid in ids):
        missing_ids = [qid for qid in ids if qid not in found]
        duplicate_ids = [qid for qid in ids if len(found.get(qid, [])) > 1]
        changed_ids = [qid for qid in ids if len(found.get(qid, [])) == 1 and
                       (len(found[qid][0]) < 9 or found[qid][0][8] != responses[qid])]
        raise RuntimeError(
            f"Chưa xác nhận lưu đủ câu trả lời ({len(found)}/{len(ids)} câu). "
            f"Thiếu: {', '.join(missing_ids) or 'không'}; "
            f"trùng: {', '.join(duplicate_ids) or 'không'}; "
            f"khác nội dung: {', '.join(changed_ids) or 'không'}. "
            "Có thể thử đồng bộ lại.")
    invalidate_records()
    return True

def queue_submission(sid, student, selected_day, order):
    """Durably mark an exam as submitted; grading runs later in Trainer mode."""
    ws = sheet("Bài kiểm tra")
    values = retry_sheets(lambda: ws.get_all_values())
    matches = [(i, row) for i, row in enumerate(values[1:], 2) if row and row[0] == sid]
    if len(matches) > 1:
        raise RuntimeError("Session ID trùng trong Bài kiểm tra.")
    if matches:
        row_num, row = matches[0]
        if len(row) > 3 and row[3] in ("Chờ chấm", "Hoàn thành"):
            return
        raise RuntimeError("Trạng thái bài kiểm tra chưa hợp lệ.")
    data = [sid, student, selected_day, "Chờ chấm", timestamp(), timestamp(),
            json.dumps(order, ensure_ascii=False), 13, len(order)-13, "", 32, timestamp()]
    append_data_rows(ws, [data])
    confirmed = retry_sheets(lambda: ws.get_all_values())
    if sum(1 for row in confirmed[1:] if row and row[0] == sid and len(row) > 3
           and row[3] == "Chờ chấm") != 1:
        raise RuntimeError("Chưa xác nhận trạng thái Chờ chấm.")
    invalidate_records()


def grade_pending_session(assignment, bank, students):
    sid = norm(assignment.get("Session ID"))
    student = norm(assignment.get("Học viên"))
    selected_day = int(assignment.get("Ngày"))
    order = json.loads(assignment.get("Danh sách câu JSON") or "[]")
    raw = retry_sheets(lambda: sheet("Câu trả lời").get_all_records(
        default_blank="", numericise_ignore=["all"]))
    matched = [r for r in raw if norm(r.get("Session ID")) == sid]
    ids = [item["id"] for item in order]
    if not ids or len(ids) != len(set(ids)) or len(matched) != len(ids):
        raise RuntimeError("Số câu trả lời gốc không khớp với bộ đề.")
    if any(norm(r.get("Họ và tên")) != student or norm(r.get("Ngày đánh giá")) != str(selected_day)
           for r in matched):
        raise RuntimeError("Câu trả lời không thuộc đúng học viên/ngày của bài kiểm tra.")
    responses = {norm(r.get("Question ID")): str(r.get("Nội dung trả lời học viên") or "") for r in matched}
    if set(responses) != set(ids) or any(not responses[qid] for qid in ids):
        raise RuntimeError("Thiếu hoặc trùng Question ID / câu trả lời.")
    # Resume a fully persisted grading result after a verification/summary error.
    # Do not call the Agent again or change already saved scores on a retry.
    saved = {norm(r.get("Question ID")): r for r in matched}
    if all(r.get("Điểm đạt") not in ("", None)
           and r.get("Điểm tối đa") not in ("", None)
           and norm(r.get("Trạng thái kiểm duyệt"))
           and norm(r.get("Câu hỏi")) == norm(bank.get(qid, {}).get("Câu hỏi"))
           and norm(r.get("Đáp án chuẩn đã duyệt")) == norm(
               bank.get(qid, {}).get("Đáp án tham khảo") or bank.get(qid, {}).get("Đáp án đề gốc"))
           for qid, r in saved.items()):
        results = []
        for item in order:
            result = dict(saved[item["id"]])
            result["Điểm đạt"] = report_number(result["Điểm đạt"])
            result["Điểm tối đa"] = report_number(result["Điểm tối đa"])
            result["review"] = item.get("review", False)
            results.append(result)
        committed = commit_results(sid, student, selected_day, order, results, students)
        update_assignment(sid, **{"Trạng thái": "Đã chấm", "Cập nhật lúc": timestamp()})
        return committed
    results = grade_submission(student, selected_day, order, bank, responses, sid)
    committed = commit_results(sid, student, selected_day, order, results, students)
    update_assignment(sid, **{"Trạng thái": "Đã chấm", "Cập nhật lúc": timestamp()})
    return committed


def grade_submission(student, selected_day, order, bank, responses, sid):
    output = []
    trainer_rules = next(
        (norm(q.get("_trainer_rules")) for q in bank.values() if norm(q.get("_trainer_rules"))),
        ""
    )
    if not trainer_rules and any(level(bank[item["id"]]) in (2, 3, 4) for item in order):
        rules = retry_sheets(lambda: sheet("Quy tắc đánh giá").get("A7:B32"))
        trainer_rules = "\n".join(" | ".join(str(v) for v in r) for r in rules if any(r))
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
            correct_points = [f"{k}. {q.get('_choices', {}).get(k, k)}"
                              for k in selected if k in q["_keys"]]
            fb = {"feedback": correct_points_feedback(correct_points),
                  "correct_points": correct_points,
                  "missing_points": [] if correct else ["Cần ôn tập câu hỏi"], "incorrect_points": []}
            needs_review = False
        else:
            q_for_grade = dict(q)
            if not norm(q_for_grade.get("_trainer_rules")):
                q_for_grade["_trainer_rules"] = trainer_rules
            score, fb = grade_open(q_for_grade, answer, maximum)
            needs_review = bool(fb.get("needs_review", False))
        if level(q) in (2, 3, 4):
            fb["feedback"] = format_agent_feedback(fb)
        else:
            fb["feedback"] = clean_feedback_language(fb.get("feedback", ""))
        output.append({"Session ID": sid, "Họ và tên": student, "Ngày": selected_day,
                       "Question ID": qid, "Ngày nội dung": q.get("Ngày nội dung"), "Module": q.get("Module"),
                       "Level": level(q), "Câu hỏi": q.get("Câu hỏi"), "Câu trả lời": answer,
                       "Đáp án tham khảo": q.get("Đáp án tham khảo") or q.get("Đáp án đề gốc"),
                       "Đánh giá từ Agent": fb.get("feedback", ""), "Đánh giá từ Agent tham khảo đáp án gợi ý": fb.get("feedback", ""),
                       "Nội dung ngoài đáp án": "\n".join(fb.get("outside_points", [])), "Điểm đạt": score,
                       "Điểm cộng/trừ": 0, "Tổng điểm": score,
                       "Điểm tối đa": maximum, "Kết quả": "Cần Trainer kiểm duyệt" if needs_review else ("Đạt" if is_passed(score, maximum) else "Chưa đạt"),
                       "Kết luận": "Đạt" if is_passed(score, maximum) else "Chưa đạt",
                       "Đánh giá từ agent": fb.get("feedback", ""),
                       "Trạng thái gửi email": "",
                       "Nội dung cần ôn": "; ".join(unique_ideas(fb.get("missing_points", []) + fb.get("incorrect_points", []))),
                       "Trang nguồn": q.get("Trang nguồn") or q.get("File nguồn"),
                       "Thời gian": timestamp(), "Trạng thái kiểm duyệt": "Agent graded - cần Trainer xem xét" if needs_review else "Agent graded",
                       "review": item.get("review", False)})
    aliases = {"Ngày đánh giá": "Ngày", "Nội dung trả lời học viên": "Câu trả lời",
               "Đáp án chuẩn đã duyệt": "Đáp án tham khảo", "Kết luận": "Kết quả",
               "Kiến thức cần ôn tập": "Nội dung cần ôn", "Nguồn đối chiếu": "Trang nguồn",
               "Thời gian trả lời": "Thời gian"}
    for row in output:
        for target, source in aliases.items():
            row[target] = row.get(source, "")
    return output

def learning_journey(results):
    fresh = [r for r in results if not r.get("review")]
    if not fresh:
        return ""
    earned = sum(float(r.get("Điểm đạt") or 0) for r in fresh)
    maximum = sum(float(r.get("Điểm tối đa") or 0) for r in fresh)
    ratio = earned / maximum if maximum else 0
    strengths = []
    gaps = []
    for lev in (1, 2, 3, 4):
        group = [r for r in fresh if int(r.get("Level") or 0) == lev]
        if not group:
            continue
        got = sum(float(r.get("Điểm đạt") or 0) for r in group)
        total = sum(float(r.get("Điểm tối đa") or 0) for r in group)
        if total and got / total >= .8:
            strengths.append(f"Level {lev}")
        elif total and got / total < PASS_RATIO:
            gaps.append(f"Level {lev}")
    lines = [f"Kết quả tổng quan: {earned:g}/{maximum:g} điểm ({ratio:.0%})."]
    lines.append("Điểm mạnh: " + (", ".join(strengths) if strengths else "Cần tiếp tục phát triển kiến thức và kỹ năng."))
    lines.append("Cần cải thiện: " + (", ".join(gaps) if gaps else "Duy trì luyện tập các nội dung đã học."))
    missing = list(dict.fromkeys(str(r.get("Kiến thức cần ôn tập") or "").strip()
                                 for r in fresh if str(r.get("Kiến thức cần ôn tập") or "").strip()))
    if missing:
        lines.append("Nội dung ưu tiên ôn tập: " + "; ".join(missing[:5]))
    lines.append("Khuyến nghị: Ôn các câu chưa đạt (điểm dưới 60%), đối chiếu đáp án Trainer và luyện tư vấn thực tế.")
    return "\n".join(lines)


def commit_results(sid, student, selected_day, order, results, students):
    # A:I was persisted before grading. Update J:S in the SAME rows.
    ws = sheet("Câu trả lời")
    existing = retry_sheets(lambda: ws.get_all_values(value_render_option="UNFORMATTED_VALUE"))
    headers = existing[0] if existing else []
    expected_ids = {item["id"] for item in order}
    result_ids = [r["Question ID"] for r in results]
    if len(result_ids) != len(expected_ids) or set(result_ids) != expected_ids:
        raise RuntimeError("Bộ kết quả thiếu hoặc trùng Question ID; không ghi điểm.")
    indexed = {}
    for idx, row in enumerate(existing[1:], 2):
        if row and row[0] == sid and len(row) > 3:
            indexed.setdefault(row[3], []).append((idx, row))
    if set(indexed) != expected_ids or any(len(v) != 1 for v in indexed.values()):
        raise RuntimeError("Thiếu hoặc trùng câu trả lời gốc; không ghi điểm tổng hợp.")
    if headers[:9] != ["Session ID", "Họ và tên", "Ngày đánh giá", "Question ID",
                        "Ngày khóa học", "Module", "Level", "Câu hỏi", "Nội dung trả lời học viên"]:
        raise RuntimeError("Cấu trúc A:I đã thay đổi.")
    updates = []
    for result in results:
        qid = result["Question ID"]
        idx, original = indexed[qid][0]
        if (len(original) < 9 or original[8] != result["Nội dung trả lời học viên"]
                or norm(original[1]) != student or norm(original[2]) != str(selected_day)):
            raise RuntimeError(f"Câu trả lời gốc {qid} không khớp; dừng chấm.")
        # Keep existing J:S column layout and Trainer review state on retries.
        graded = [result.get(h) if result.get(h) is not None else "" for h in headers[9:]]
        if "Điểm theo đáp án" in result:
            graded[headers.index("Điểm đạt") - 9] = result["Điểm theo đáp án"]
        if norm(dict(zip(headers, original)).get("Trạng thái kiểm duyệt")).casefold() == "trainer approved" and "Điểm theo đáp án" not in result:
            continue
        updates.append({"range": f"J{idx}:{gspread.utils.rowcol_to_a1(idx, len(headers))}",
                        "values": [graded]})
    if updates:
        retry_sheets(lambda: ws.batch_update(updates, value_input_option="RAW"))
    verified = retry_sheets(lambda: ws.get_all_values(value_render_option="UNFORMATTED_VALUE"))
    for qid, entries in indexed.items():
        idx = entries[0][0]
        row = verified[idx-1]
        if row[:9] != entries[0][1][:9]:
            raise RuntimeError(f"Dữ liệu gốc của câu {qid} đã thay đổi; dừng hoàn tất bài.")
        if any(len(row) <= headers.index(h) or row[headers.index(h)] in ("", None) for h in ("Điểm đạt", "Điểm tối đa")):
            raise RuntimeError(f"Chưa xác minh điểm của câu {qid}.")
    for update in updates:
        idx = int(re.search(r"\d+", update["range"]).group())
        actual = [v if v is not None else "" for v in verified[idx-1][9:len(headers)]]
        actual += [""] * (len(headers)-9-len(actual))
        if actual != update["values"][0]:
            raise RuntimeError("Kết quả đọc lại không khớp dữ liệu chấm; chưa hoàn tất bài.")
    invalidate_records()
    fresh_score = sum(float(r["Điểm đạt"]) for r in results if not r["review"])
    sws = sheet("Lượt đánh giá")
    summary = retry_sheets(lambda: sws.get_all_values())
    dealer = next((x.get("Đại lý", "") for x in students if norm(x.get("Họ và tên")) == student), "")
    summary_headers = summary[0]
    journey = learning_journey(results)
    review_topics = list(dict.fromkeys(
        str(r.get("Kiến thức cần ôn tập") or "").strip()
        for r in results if not r.get("review") and str(r.get("Kiến thức cần ôn tập") or "").strip()))
    plan = "Ưu tiên ôn tập: " + "; ".join(review_topics[:5]) if review_topics else "Tiếp tục luyện tập và củng cố kiến thức."
    by_level = {}
    for lev in (2, 3, 4):
        group = [r for r in results if not r.get("review") and int(r.get("Level") or 0) == lev]
        got = sum(float(r.get("Điểm đạt") or 0) for r in group)
        total = sum(float(r.get("Điểm tối đa") or 0) for r in group)
        by_level[f"Điểm Level {lev} (%)"] = round(got / total * 100, 2) if total else ""
    values = {"Session ID": sid, "Họ và tên": student, "Đại lý": dealer,
              "Ngày đánh giá": selected_day, "Lần làm": 1,
              "Bắt đầu lúc": timestamp(), "Nộp lúc": timestamp(),
              "Số câu": len(order), "Điểm đạt": fresh_score, "Điểm tối đa": 32,
              "Tỷ lệ (%)": round(fresh_score / 32 * 100, 2),
              "Trạng thái": "Completed", "Hành trình học tập": journey,
              "Nội dung cần ôn tập cho ngày tiếp theo": plan, **by_level}
    summary_matches = [i for i, r in enumerate(summary[1:], 2) if r and r[0] == sid]
    if len(summary_matches) > 1:
        raise RuntimeError("Session ID trùng trong Lượt đánh giá.")
    if summary_matches:
        old_summary = dict(zip(summary_headers, summary[summary_matches[0]-1]))
        for field in ("Bắt đầu lúc", "Nộp lúc", "Lần làm"):
            if old_summary.get(field):
                values[field] = old_summary[field]
    summary_data = [values.get(h, "") for h in summary_headers]
    if summary_matches:
        n = summary_matches[0]
        retry_sheets(lambda: sws.batch_update([{"range": f"A{n}:{gspread.utils.rowcol_to_a1(n, len(summary_headers))}", "values": [summary_data]}], value_input_option="RAW"))
    else:
        append_data_rows(sws, [summary_data])
    confirmed_summary = retry_sheets(lambda: sws.get_all_values())
    if sum(1 for row in confirmed_summary[1:] if row and row[0] == sid) != 1:
        raise RuntimeError("Chưa xác minh lưu duy nhất báo cáo tổng hợp.")
    aws = sheet("Bài kiểm tra")
    assignment_rows = retry_sheets(lambda: aws.get_all_values())
    idx = next((i for i, row in enumerate(assignment_rows[1:], 2) if row and row[0] == sid), None)
    if idx is None:
        # Single append of assignment metadata after grading.
        append_data_rows(aws, [[sid, student, selected_day, "Hoàn thành", timestamp(), timestamp(), json.dumps(order, ensure_ascii=False), 13, len(order)-13, fresh_score, 32, timestamp()]])
    else:
        cols = {name: i+1 for i, name in enumerate(ASSIGN_HEADERS)}
        updates = [{"range": gspread.utils.rowcol_to_a1(idx, cols[k]), "values": [[v]]} for k, v in {"Trạng thái":"Hoàn thành", "Kết thúc":timestamp(), "Điểm mới":fresh_score, "Cập nhật lúc":timestamp()}.items()]
        retry_sheets(lambda: aws.batch_update(updates, value_input_option="RAW"))
    invalidate_records()
    return fresh_score


# Single-stage Agent grading. A:I are immutable learner evidence.
V2_HEADERS = ["Session ID", "Họ và tên", "Ngày đánh giá", "Question ID", "Ngày khóa học", "Module", "Level", "Câu hỏi", "Nội dung trả lời học viên", "Đáp án chuẩn đã duyệt", "Đánh giá từ Agent tham khảo đáp án gợi ý", "Điểm đạt", "Điểm cộng/trừ", "Tổng điểm", "Điểm tối đa", "Kết luận", "Kiến thức cần ôn tập", "Nguồn đối chiếu", "Thời gian trả lời", "Trạng thái kiểm duyệt", "Trạng thái gửi email"]


def unique_ideas(items):
    result, seen = [], set()
    for item in items:
        text = str(item).strip().lstrip("• ").strip()
        key = " ".join(text.casefold().split())
        if key and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def effective_score(row):
    value = row.get("Tổng điểm")
    return value if value not in (None, "") else row.get("Điểm đạt")


def effective_comment(row):
    return (row.get("Đánh giá từ agent") or row.get("Đánh giá từ Agent")
            or row.get("Đánh giá từ Agent tham khảo đáp án gợi ý"))


TRAINER_CC = "daotao.sanpham@gmail.com"

def send_report_email(recipient, student, dealer, selected_day, rows):
    """Send Trainer-reviewed report using SMTP settings in Streamlit Secrets."""
    host = st.secrets.get("SMTP_HOST")
    username = st.secrets.get("SMTP_USERNAME")
    password = st.secrets.get("SMTP_PASSWORD")
    port = int(st.secrets.get("SMTP_PORT", 465))
    if not all((host, username, password)):
        raise RuntimeError("Cần cấu hình SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD trong Streamlit Secrets")
    sid = norm(rows[0].get("Session ID")) if rows else ""
    stored_assignment = next((a for a in records("Bài kiểm tra") if norm(a.get("Session ID")) == sid), {})
    review_ids = {item["id"] for item in json.loads(stored_assignment.get("Danh sách câu JSON") or "[]") if item.get("review")}
    # Use all recorded sessions, not just the current report, for repeat warnings.
    repeat_alerts = [
        item for item in repeated_unpassed_questions(records("Câu trả lời"), records("Học viên"))
        if item["Học viên"] == student
    ]
    # Google Sheets may return Vietnamese decimals as "1,6".
    # Only the 13 new questions contribute to the 32-point total.
    earned = sum(report_number(effective_score(r)) for r in rows
                 if norm(r.get("Question ID")) not in review_ids and not norm(r.get("Loại câu hỏi")).lower().startswith("ôn tập"))
    maximum = sum(report_number(r.get("Điểm tối đa")) for r in rows
                  if norm(r.get("Question ID")) not in review_ids and not norm(r.get("Loại câu hỏi")).lower().startswith("ôn tập"))
    lines = [f"BÁO CÁO HỌC TẬP - NGÀY {selected_day}",
             f"Học viên: {student}", f"Đại lý: {dealer}", f"Điểm: {earned:g}/{maximum:g}", "",
             "Khuyến nghị: Xem chi tiết phần nhận xét của Trainer, các câu chưa đạt (điểm dưới 60%), đối chiếu đáp án Trainer để làm tốt hơn.", ""]
    if repeat_alerts:
        lines.extend(["", "KIẾN THỨC CẦN ƯU TIÊN ÔN TẬP"])
        for alert in repeat_alerts:
            lines.append(f'- {alert["Câu hỏi"]} ({alert["Số lần chưa đạt"]} lần chưa đạt; điểm: {alert["Điểm từng lần"]})')
        lines.append("")
    for i, r in enumerate(rows, 1):
        q = bank.get(norm(r.get("Question ID")), {})
        is_l1 = norm(r.get("Level")) == "1"
        answer = norm(r.get("Nội dung trả lời học viên"))
        if is_l1:
            choices = q.get("_choices", {})
            def describe_keys(raw):
                keys = re.findall(r"[A-D]", raw.upper())
                return "; ".join(f"{k}. {choices[k]}" if k in choices else k for k in keys) or raw
            answer = describe_keys(answer)
            lines.extend([f"Câu {i}: {norm(r.get('Câu hỏi'))}",
                          f"Câu trả lời của bạn: {answer}", ""])
        else:
            fb = report_feedback(effective_comment(r))
            lines.extend([f"Câu {i}: {norm(r.get('Câu hỏi'))}",
                          f"Câu trả lời của bạn: {answer}",
                          f"Điểm: {report_number(effective_score(r)):g}/{report_number(r.get('Điểm tối đa')):g}",
                          f"Nhận xét: {norm(fb.get('feedback'))}", ""])

    # HTML email: readable on Gmail/mobile; text/plain remains as fallback.
    from html import escape as html_escape
    def h(value):
        return (html_escape(str(value or ""))
                .replace("\r\n", "<br>")
                .replace("\r", "<br>")
                .replace("\n", "<br>"))
    sections = []
    for i, r in enumerate(rows, 1):
        q = bank.get(norm(r.get("Question ID")), {})
        is_l1 = norm(r.get("Level")) == "1"
        answer = norm(r.get("Nội dung trả lời học viên"))
        if is_l1:
            choices = q.get("_choices", {})
            def expand(raw):
                keys = re.findall(r"[A-D]", raw.upper())
                return "; ".join(f"{k}. {choices[k]}" if k in choices else k for k in keys) or raw
            answer = expand(answer)
        detail = (
            f'<div style="margin:12px 0;line-height:1.65">'
            f'<span style="font-weight:600;color:#475569">Câu trả lời của bạn:</span><br>{h(answer)}</div>'
        )
        if not is_l1:
            feedback = norm(report_feedback(effective_comment(r)).get("feedback"))
            detail += (
                f'<div style="margin:14px 0;line-height:1.65">'
                f'<span style="font-weight:600;color:#475569">Điểm:</span> '
                f'{report_number(effective_score(r)):g}/{report_number(r.get("Điểm tối đa")):g}</div>'
                f'<div style="padding:14px 16px;background:#f0f7fc;border-left:3px solid #1473a5;'
                f'border-radius:5px;margin:16px 0"><strong style="color:#075985">Nhận xét Trainer</strong>'
                f'<div style="margin-top:10px;line-height:1.8;color:#263445">{h(feedback)}</div></div>'
            )
        sections.append(
            f'<div style="border:1px solid #e2e8f0;border-radius:10px;padding:18px 16px;'
            f'margin:18px 0;background:#ffffff">'
            f'<h3 style="margin:0 0 14px;color:#123b70;font-size:17px;line-height:1.45">'
            f'Câu {i}. {h(r.get("Câu hỏi"))}</h3>{detail}</div>'
        )
    review_html = ""
    if repeat_alerts:
        review_items = "".join(
            '<li style="margin:10px 0"><strong>' + h(item["Câu hỏi"]) + '</strong>'
            + '<div style="margin-top:4px">Chưa đạt: ' + str(item["Số lần chưa đạt"])
            + ' lần · Điểm từng lần: ' + h(item["Điểm từng lần"]) + '</div></li>'
            for item in repeat_alerts
        )
        review_html = (
            '<div style="padding:16px;background:#fff7ed;border:1px solid #fdba74;'
            'border-radius:8px;margin:16px 0;line-height:1.6">'
            '<strong style="color:#9a3412">Kiến thức cần ưu tiên ôn tập</strong>'
            '<p>Những câu hỏi đã trả lời nhiều lần nhưng lần gần nhất vẫn chưa đạt:</p>'
            '<ol style="padding-left:20px;margin:8px 0">' + review_items + '</ol></div>'
        )

    html_body = (
        '<html><body style="margin:0;padding:24px 12px;background:#f5f7fb;'
        'font-family:Arial,Helvetica,sans-serif;color:#1e293b">'
        '<div style="max-width:720px;margin:auto">'
        '<div style="background:#123b70;color:white;padding:24px;border-radius:10px">'
        f'<h1 style="font-size:22px;margin:0 0 12px;color:white">BÁO CÁO HỌC TẬP - NGÀY {selected_day}</h1>'
        f'<div style="font-size:16px">Học viên: <strong>{h(student)}</strong></div>'
        f'<div style="font-size:15px;margin-top:6px">Đại lý: <strong>{h(dealer)}</strong></div>'
        f'<div style="font-size:19px;margin-top:10px">Điểm: <strong>{earned:g}/{maximum:g}</strong></div>'
        '</div>'
        '<div style="padding:16px;background:#eaf2fc;border-radius:8px;margin:16px 0;line-height:1.6">'
        '<strong>Khuyến nghị:</strong> Xem chi tiết phần nhận xét của Trainer, '
        'các câu chưa đạt (điểm dưới 60%), đối chiếu đáp án Trainer để làm tốt hơn.</div>'
        + review_html + "".join(sections) + '</div></body></html>'
    )
    message = EmailMessage()
    message["Subject"] = f"FA Knowledge Coach - Báo cáo Ngày {selected_day} - {student}"
    message["From"] = st.secrets.get("SMTP_FROM", username)
    message["To"] = recipient
    message["Cc"] = TRAINER_CC
    message.set_content("\n".join(lines))
    message.add_alternative(html_body, subtype="html")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)

def clean_feedback_language(value):
    """Remove grading-material references from learner-facing comments."""
    result = str(value or "")
    result = re.sub(
        r"(?:không có|không được đề cập|không nằm) trong đáp án tham khảo",
        "cần được kiểm chứng thêm",
        result,
        flags=re.IGNORECASE,
    )
    # Remove wording such as “mà đáp án tham khảo đã nêu” from the feedback itself.
    result = re.sub(
        r"\s*(?:(?:mà|như|theo|so với|trong|dựa trên|phù hợp với|tương đương với)\s+)?"
        r"đáp án (?:tham khảo|mẫu|chuẩn)(?:\s+đã\s+nêu)?",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(r"\s+([,.])", r"\1", result)
    result = re.sub(r" {2,}", " ", result)
    return result.strip()


def format_agent_feedback(feedback):
    """Render concise learner feedback with blank lines between sections."""
    if not isinstance(feedback, dict):
        return clean_feedback_language(feedback)
    groups = [
        ("Các ý đã trình bày đúng", ("correct_points",)),
        ("Các ý có thể bổ sung", ("missing_points",)),
        ("Ý hay được cộng thêm điểm", ("outside_correct_points",)),
        ("Ý sai/không phù hợp", ("incorrect_points", "outside_wrong_points")),
    ]
    blocks = []
    for title, fields in groups:
        items = []
        for field in fields:
            values = feedback.get(field, [])
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                continue
            for value in values:
                item = clean_feedback_language(value).lstrip("•- ").strip()
                if item and item not in items:
                    items.append(item)
        if not items:
            continue
        block = [title + ":"]
        block.extend("- " + item for item in items[:5])
        if title == "Các ý có thể bổ sung":
            block.append("Bạn có thể bổ sung các ý trên để phần tư vấn thuyết phục hơn.")
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)

def report_feedback(value):
    """Support both JSON Agent feedback and Trainer-edited plain text."""
    if isinstance(value, dict):
        result = value.copy()
    else:
        raw = norm(value)
        try:
            obj = json.loads(raw)
            result = obj if isinstance(obj, dict) else {"feedback": raw}
        except (ValueError, TypeError):
            result = {"feedback": raw}
    if isinstance(result.get("feedback"), str):
        result["feedback"] = clean_feedback_language(result["feedback"])
    return result


def trainer_approved(rows):
    return bool(rows) and all(norm(r.get("Trạng thái kiểm duyệt")).casefold() == "trainer approved" for r in rows)

def report_number(value):
    try:
        return float(str(value or "0").replace(",", "."))
    except (ValueError, TypeError):
        return 0.0

def show_student_report(student, selected_day, history):
    """Read final values from Câu trả lời; never expose Trainer action plans."""
    from datetime import date
    rows = [r for r in history
            if norm(r.get("Họ và tên") or r.get("Học viên")) == student
            and norm(r.get("Ngày đánh giá") or r.get("Ngày")) in (str(selected_day), f"Ngày {selected_day}")]
    if not rows:
        st.info("Chưa có báo cáo cho học viên và ngày đã chọn.")
        return

    # When the same student has multiple attempts, display only the latest session.
    sid = norm(rows[-1].get("Session ID"))
    if sid:
        rows = [r for r in rows if norm(r.get("Session ID")) == sid]

    if not trainer_approved(rows):
        st.info("Báo cáo học tập sẽ được gửi về e-mail sau khi Giảng viên rà soát.")
        return

    # Use submission timestamp, not training day number, for next-day release.
    dated = []
    for r in rows:
        raw = norm(r.get("Thời gian trả lời"))
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
            try:
                dated.append(datetime.strptime(raw[:19], fmt).date())
                break
            except ValueError:
                pass
    if dated and max(dated) >= datetime.now(TZ).date():
        st.info("Báo cáo học tập sẽ được gửi về e-mail sau khi Giảng viên rà soát.")
        return

    graded = [r for r in rows if effective_score(r) not in ("", None) and norm(r.get("Điểm tối đa"))]
    if not graded:
        st.info("Báo cáo đang được chấm và hoàn thiện.")
        return

    stored_assignment = next((a for a in records("Bài kiểm tra") if norm(a.get("Session ID")) == sid), {})
    review_ids = {item["id"] for item in json.loads(stored_assignment.get("Danh sách câu JSON") or "[]") if item.get("review")}
    fresh_graded = [r for r in graded if norm(r.get("Question ID")) not in review_ids]
    earned = sum(report_number(effective_score(r)) for r in fresh_graded)
    maximum = sum(report_number(r.get("Điểm tối đa")) for r in fresh_graded)
    st.subheader("Tổng quan kết quả")
    c1, c2 = st.columns(2)
    c1.metric("Điểm đạt", f"{earned:g}/{maximum:g}")
    c2.metric("Số câu đã chấm", f"{len(graded)}")

    journey = next((norm(r.get("Hành trình học tập")) for r in records("Lượt đánh giá")
                    if norm(r.get("Session ID")) == sid), "")
    st.subheader("Hành trình học tập")
    if journey:
        st.write(journey)
    else:
        st.info("Trainer đang hoàn thiện phần Hành trình học tập.")

    review = list(dict.fromkeys(norm(r.get("Kiến thức cần ôn tập")) for r in rows
                                if norm(r.get("Kiến thức cần ôn tập"))))
    if review:
        st.subheader("Kiến thức cần ôn tập")
        for item in review:
            st.write("•", item)

    st.subheader("Chi tiết từng câu")
    for i, r in enumerate(rows, 1):
        qid = norm(r.get("Question ID"))
        score = str(effective_score(r)) if effective_score(r) not in (None, "") else ""
        total = norm(r.get("Điểm tối đa"))
        with st.expander(f"Câu {i} · Level {norm(r.get('Level'))} · {score or '—'}/{total or '—'} điểm"):
            if qid:
                st.caption(f"Question ID: {qid}")
            panel("Câu hỏi", r.get("Câu hỏi"), "question")
            panel("Câu trả lời của học viên",
                  r.get("Nội dung trả lời học viên") or r.get("Câu trả lời"), "answer")
            fb = report_feedback(effective_comment(r))
            comment = fb.get("feedback")
            if comment:
                panel("Nhận xét", comment, "feedback")
            if norm(r.get("Kiến thức cần ôn tập")):
                st.caption("Cần ôn: " + norm(r.get("Kiến thức cần ôn tập")))


def set_exam_open(selected_day, is_open):
    """Persist the Trainer-controlled exam switch in column E of Khóa học."""
    ws = sheet("Khóa học")
    values = retry_sheets(lambda: ws.get_all_values())
    if not values:
        raise RuntimeError("Tab Khóa học trống.")
    if len(values[0]) < 5 or norm(values[0][4]) != "Trạng thái bài kiểm tra":
        retry_sheets(lambda: ws.update("E1", [["Trạng thái bài kiểm tra"]]))
    matches = []
    for idx, row in enumerate(values[1:], 2):
        match = re.search(r"(\d+)", norm(row[0])) if row else None
        if match and int(match.group(1)) == selected_day:
            matches.append(idx)
    if len(matches) != 1:
        raise RuntimeError("Không xác định duy nhất ngày đào tạo trong Khóa học.")
    retry_sheets(lambda: ws.update(f"E{matches[0]}", [["Mở" if is_open else "Đóng"]]))
    cached_course_values.clear()
    invalidate_records()


def course_window():
    """Only the Trainer's switch controls availability; dates/times are advisory."""
    now = datetime.now(TZ)
    schedule = []
    for row in cached_course_values()[1:]:
        if not row:
            continue
        match = re.search(r"(\d+)", norm(row[0]))
        if not match:
            continue
        schedule.append((int(match.group(1)), None, None, norm(row[4]) if len(row) > 4 else "Đóng"))
    active = next((item for item in schedule if item[3].casefold() == "mở"), None)
    return now, active, sorted(schedule, key=lambda item: item[0])


def parse_course_date(value, today=None):
    """Parse the date formats used in Khóa học without deprecated day-month parsing."""
    today = today or datetime.now(TZ).date()
    raw = norm(value)
    if not raw:
        return None
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.strptime(f"{raw}-{today.year}", "%d-%b-%Y").date()
    except ValueError:
        return None


def sheet_timestamp_date(value):
    """Convert an ISO or date-only Sheet value to a local calendar date."""
    raw = norm(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(TZ)
        return parsed.date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y", "%d-%b"):
        try:
            text = raw if fmt != "%d-%b" else f"{raw}-{datetime.now(TZ).year}"
            return datetime.strptime(text, fmt if fmt != "%d-%b" else "%d-%b-%Y").date()
        except ValueError:
            pass
    return None


def course_date_range():
    """Return the inclusive start/end dates configured in Khóa học."""
    today = datetime.now(TZ).date()
    dates = []
    for row in cached_course_values()[1:]:
        if len(row) > 1:
            parsed = parse_course_date(row[1], today)
            if parsed:
                dates.append(parsed)
    return (min(dates), max(dates)) if dates else (None, None)


def within_course_period(value, start_date, end_date):
    parsed = sheet_timestamp_date(value)
    return bool(parsed and start_date and end_date and start_date <= parsed <= end_date)


def course_session_ids(assignments, start_date, end_date, selected_day=None):
    result = set()
    for row in assignments:
        if selected_day is not None and norm(row.get("Ngày")) != str(selected_day):
            continue
        started = norm(row.get("Bắt đầu")) or norm(row.get("Cập nhật lúc"))
        if within_course_period(started, start_date, end_date):
            sid = norm(row.get("Session ID"))
            if sid:
                result.add(sid)
    return result

def suggested_training_day():
    """Suggest the course day from the date column, without opening an exam."""
    today = datetime.now(TZ).date()
    try:
        rows = cached_course_values()[1:]
        dated = []
        for row in rows:
            if len(row) < 2:
                continue
            match = re.search(r"(\d+)", norm(row[0]))
            if not match:
                continue
            d = parse_course_date(row[1], today)
            if d is None:
                continue
            day_number = int(match.group(1))
            if 1 <= day_number <= 4:
                dated.append((day_number, d))
        today_matches = [day_number for day_number, d in dated if d == today]
        if today_matches:
            return today_matches[0]
        previous = [(d, day_number) for day_number, d in dated if d < today]
        if previous:
            return max(previous)[1]
        if dated:
            return min((d, day_number) for day_number, d in dated)[1]
    except Exception:
        pass
    return 1


with st.sidebar:
    role = st.radio("Giao diện", ["Học viên", "Trainer"], key="fa_role")
    if role == "Trainer":
        password = st.secrets.get("TRAINER_PASSWORD")
        if not password or st.text_input("Mật khẩu Trainer", type="password") != password:
            st.stop()
        mode = st.radio("Chức năng", ["Trainer – Dashboard", "Trainer – Điều khiển bài kiểm tra", "Trainer – Chấm bài", "Trainer – Test học viên", "Trainer – Xem trước", "Trainer – Cảnh báo", "Trainer – Duyệt & gửi tất cả"])
    else:
        mode = "Làm bài kiểm tra"
    if role == "Trainer":
        selected_day = st.selectbox("Ngày đào tạo", [1, 2, 3, 4], index=suggested_training_day()-1, format_func=lambda d: f"Ngày {d}")
        st.caption("13 câu mới/ngày · 32 điểm; câu ôn tập không cộng điểm")
    else:
        try:
            now, active_window, course_schedule = course_window()
        except Exception as exc:
            st.error(f"Lịch kiểm tra chưa hợp lệ: {exc}")
            st.stop()
        selected_day = active_window[0] if active_window else None

# Avoid loading data on every widget change: the full assessment uses one st.form.
try:
    students, bank, history, assignments = get_initial_data("Trainer" if role == "Trainer" and mode != "Trainer – Chấm bài" and (mode != "Trainer – Dashboard" or st.session_state.get(f"trainer_report_requested:{selected_day}", False)) else "Học viên")
except Exception as exc:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        st.warning("Google Sheets đang giới hạn lượt truy cập. Chờ khoảng một phút rồi bấm Thử tải lại; tránh tải lại liên tục.")
    else:
        st.error(f"Không thể tải dữ liệu Google Sheets: {type(exc).__name__}: {exc}")
    with st.expander("Chi tiết lỗi để kiểm tra"):
        st.code(str(exc))
    if st.button("Thử tải lại dữ liệu"):
        st.rerun()
    st.stop()

names = [norm(x.get("Họ và tên")) for x in students if norm(x.get("Họ và tên"))]
if not names:
    st.warning("Chưa có học viên trong tab Học viên")
    st.stop()
student = st.selectbox("Học viên", names)
student_info = next((x for x in students if norm(x.get("Họ và tên")) == student), {})
st.caption("Đại lý: " + norm(student_info.get("Đại lý")))
if role == "Học viên":
    current_email = norm(student_info.get("Email"))
    with st.expander("Email nhận báo cáo (bắt buộc)", expanded=not bool(current_email)):
        if current_email:
            st.info("Email đã đăng ký: " + current_email + ". Liên hệ Trainer nếu cần thay đổi.")
        else:
            with st.form("email_registration"):
                new_email = st.text_input("Email", placeholder="ten@example.com")
                save_email = st.form_submit_button("Lưu email")
            if save_email:
                if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", new_email.strip()):
                    st.error("Email không hợp lệ.")
                else:
                    ws = sheet("Học viên")
                    data = retry_sheets(lambda: ws.get_all_values())
                    headers = data[0]
                    col = headers.index("Email") + 1
                    row_index = next((i for i, row in enumerate(data[1:], 2) if row and norm(row[0]) == student), None)
                    if row_index is None:
                        st.error("Không tìm thấy học viên.")
                    elif len(data[row_index-1]) >= col and norm(data[row_index-1][col-1]):
                        st.warning("Email đã tồn tại. Liên hệ Trainer để thay đổi.")
                    else:
                        retry_sheets(lambda: ws.update_cell(row_index, col, new_email.strip()))
                        invalidate_records("Học viên")
                        st.success("Đã lưu email vào Google Sheets.")
                        st.rerun()

    if not current_email:
        st.warning("Bạn cần đăng ký email nhận báo cáo trước khi làm bài kiểm tra.")
        st.stop()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", current_email):
        st.error("Email đã đăng ký chưa hợp lệ. Vui lòng liên hệ Giảng viên để cập nhật trước khi làm bài.")
        st.stop()

if mode == "Xem báo cáo học tập":
    # Refresh explicitly to pick up Trainer edits without reloading on every widget interaction.
    if st.button("Làm mới báo cáo"):
        invalidate_records()
        st.rerun()
    show_student_report(student, selected_day, history)
    st.stop()

def learner_progress_data(attempts, answer_history, students):
    """Latest completed attempt per training day; repeated-question recovery by session order."""
    directory = {norm(x.get("Họ và tên")): norm(x.get("Đại lý")) for x in students}
    daily = {}
    for index, row in enumerate(attempts):
        name = norm(row.get("Họ và tên"))
        raw_day = norm(row.get("Ngày đánh giá")).replace("Ngày ", "")
        if not name or raw_day not in ("1", "2", "3", "4"):
            continue
        if norm(row.get("Trạng thái")).casefold() not in ("completed", "hoàn thành"):
            continue
        daily[(name, int(raw_day))] = (index, row)
    by_student = {}
    for (name, training_day), (_, row) in daily.items():
        by_student.setdefault(name, {})[training_day] = row

    question_history = {}
    for row in answer_history:
        name, qid, sid = (norm(row.get(key)) for key in ("Họ và tên", "Question ID", "Session ID"))
        if name and qid and sid:
            question_history.setdefault((name, qid), {})[sid] = row

    recovered = {}
    unresolved = {}
    for (name, _), sessions in question_history.items():
        entries = list(sessions.values())
        if len(entries) < 2:
            continue
        statuses = [norm(x.get("Kết luận")).casefold() for x in entries]
        if statuses[-1] == "đạt" and "chưa đạt" in statuses[:-1]:
            recovered[name] = recovered.get(name, 0) + 1
        if statuses[-1] == "chưa đạt" and statuses.count("chưa đạt") >= 2:
            unresolved[name] = unresolved.get(name, 0) + 1

    result = []
    for name, days in sorted(by_student.items()):
        ordered = sorted(days)
        first, last = days[ordered[0]], days[ordered[-1]]
        first_pct = report_number(first.get("Tỷ lệ (%)"))
        last_pct = report_number(last.get("Tỷ lệ (%)"))
        delta = last_pct - first_pct if len(ordered) >= 2 else None
        if delta is None:
            trend = "Chưa đủ dữ liệu"
        elif delta >= 5:
            trend = "Tiến bộ"
        elif delta <= -5:
            trend = "Cần hỗ trợ"
        else:
            trend = "Ổn định"
        result.append({
            "Học viên": name, "Đại lý": directory.get(name, norm(last.get("Đại lý"))),
            "Số ngày đã học": len(ordered),
            "Ngày 1 (%)": round(report_number(days[1].get("Tỷ lệ (%)")), 1) if 1 in days else None,
            "Ngày 2 (%)": round(report_number(days[2].get("Tỷ lệ (%)")), 1) if 2 in days else None,
            "Ngày 3 (%)": round(report_number(days[3].get("Tỷ lệ (%)")), 1) if 3 in days else None,
            "Ngày 4 (%)": round(report_number(days[4].get("Tỷ lệ (%)")), 1) if 4 in days else None,
            "Thay đổi (điểm %)": round(delta, 1) if delta is not None else None,
            "L2 thay đổi": round(report_number(last.get("Điểm Level 2 (%)")) - report_number(first.get("Điểm Level 2 (%)")), 1) if delta is not None else None,
            "L3 thay đổi": round(report_number(last.get("Điểm Level 3 (%)")) - report_number(first.get("Điểm Level 3 (%)")), 1) if delta is not None else None,
            "L4 thay đổi": round(report_number(last.get("Điểm Level 4 (%)")) - report_number(first.get("Điểm Level 4 (%)")), 1) if delta is not None else None,
            "Đã khắc phục": recovered.get(name, 0),
            "Chưa khắc phục": unresolved.get(name, 0),
            "Xu hướng": trend,
        })
    return result


def repeated_unpassed_questions(history, students):
    """Detect repeated attempts with latest Kết luận still Chưa đạt."""
    directory = {norm(x.get("Họ và tên")): norm(x.get("Đại lý")) for x in students}
    groups = {}
    for index, row in enumerate(history):
        name, qid, sid = (norm(row.get(k)) for k in ("Họ và tên", "Question ID", "Session ID"))
        if name and qid and sid:
            groups.setdefault((name, qid), {})[sid] = (index, row)
    warnings = []
    for (name, qid), sessions in groups.items():
        attempts = [r for _, r in sorted(sessions.values(), key=lambda item: item[0])]
        failed = [r for r in attempts if norm(r.get("Kết luận")).casefold() == "chưa đạt"]
        if len(attempts) < 2 or len(failed) < 2:
            continue
        if norm(attempts[-1].get("Kết luận")).casefold() != "chưa đạt":
            continue
        warnings.append({
            "Học viên": name, "Đại lý": directory.get(name, ""),
            "Question ID": qid, "Câu hỏi": norm(attempts[-1].get("Câu hỏi")),
            "Số lần trả lời": len(attempts), "Số lần chưa đạt": len(failed),
            "Ngày đánh giá": " → ".join(norm(r.get("Ngày đánh giá")) for r in attempts),
            "Điểm từng lần": " → ".join(
                f'{norm(effective_score(r))}/{norm(r.get("Điểm tối đa"))}' for r in attempts),
            "Kết luận từng lần": " → ".join(norm(r.get("Kết luận")) for r in attempts),
        })
    return sorted(warnings, key=lambda r: (-r["Số lần chưa đạt"], r["Học viên"], r["Question ID"]))


if mode.startswith("Trainer"):
    if mode == "Trainer – Test học viên":
        st.subheader("Test giao diện học viên")
        st.caption("Chọn học viên từ danh sách để thử đúng giao diện làm bài. Không ghi dữ liệu test vào Google Sheets hoặc gửi email.")
        test_student = st.selectbox("Học viên test", names, key="trainer_test_student")
        test_info = next((r for r in students if norm(r.get("Họ và tên")) == test_student), {})
        st.write("**Đại lý:** " + norm(test_info.get("Đại lý")))
        st.write("**Email:** " + (norm(test_info.get("Email")) or "Chưa cập nhật"))
        test_key = f"trainer_test:{test_student}:{selected_day}"
        if test_key not in st.session_state:
            if st.button("Bắt đầu bài kiểm tra", type="primary"):
                proposed, shortage = build_assignment(test_student, selected_day, bank, [])
                if shortage:
                    st.error("Không đủ câu hỏi hợp lệ để tạo bài kiểm tra.")
                else:
                    st.session_state[test_key] = {"sid": str(uuid.uuid4()), "order": proposed, "status": "taking", "responses": None}
                    st.rerun()
            st.stop()
        test = st.session_state[test_key]
        if test["status"] == "taking":
            order = test["order"]
            with st.form(f"trainer_test_form:{test_key}"):
                responses = {}
                for i, item in enumerate(order, 1):
                    q = bank[item["id"]]
                    st.markdown(f"### Câu {i}/{len(order)}")
                    panel("Câu hỏi", q.get("Câu hỏi", ""), "question")
                    if level(q) == 1:
                        keys = sorted(q["_choices"])
                        seed = int(hashlib.sha256(f"{test['sid']}|{item['id']}".encode()).hexdigest(), 16)
                        random.Random(seed).shuffle(keys)
                        if len(q["_keys"]) > 1:
                            chosen = [k for k in keys if st.checkbox(f"{k}. {q['_choices'][k]}", key=f"{test_key}:{item['id']}:{k}")]
                        else:
                            chosen_one = st.radio("Chọn đáp án", keys, index=None,
                                                  key=f"{test_key}:{item['id']}",
                                                  format_func=lambda k: f"{k}. {q['_choices'][k]}")
                            chosen = [chosen_one] if chosen_one else []
                        responses[item["id"]] = ",".join(sorted(chosen))
                    else:
                        responses[item["id"]] = st.text_area("Câu trả lời", key=f"{test_key}:{item['id']}", height=240)
                submitted = st.form_submit_button("Nộp bài kiểm tra", type="primary")
            if submitted:
                if any(not responses.get(item["id"], "").strip() for item in order):
                    st.warning("Bạn còn câu chưa trả lời. Vui lòng hoàn thành trước khi nộp.")
                else:
                    test["responses"] = responses
                    test["status"] = "grading"
                    st.rerun()
        elif test["status"] == "grading":
            with st.spinner("Đang chấm điểm và tạo nhận xét..."):
                try:
                    test["results"] = grade_submission(test_student, selected_day, test["order"], bank, test["responses"], test["sid"])
                    test["status"] = "saved"
                    st.rerun()
                except Exception as exc:
                    st.error(f"Chấm bài test chưa thành công: {exc}")
        else:
            results = test.get("results") or []
            result_by_id = {norm(r.get("Question ID")): r for r in results}
            total = sum(float(r.get("Điểm đạt") or 0) for r in results)
            maximum = sum(float(r.get("Điểm tối đa") or 0) for r in results)
            st.success("Đã chấm xong bài test.")
            st.metric("Tổng điểm", f"{total:g}/{maximum:g}", f"{(total / maximum):.0%}" if maximum else None)
            st.caption("Chế độ Trainer test: không ghi dữ liệu vào Google Sheets và không gửi email.")
            for idx, item in enumerate(test["order"], 1):
                q = bank[item["id"]]
                result = result_by_id.get(item["id"], {})
                st.markdown(f"### Câu {idx}/{len(test['order'])}")
                panel("Câu hỏi", norm(q.get("Câu hỏi")), "question")
                panel("Câu trả lời", norm(result.get("Câu trả lời") or (test.get("responses") or {}).get(item["id"])), "answer")
                score = float(result.get("Điểm đạt") or 0)
                max_points = float(result.get("Điểm tối đa") or POINTS[level(q)])
                st.write(f"**Điểm:** {score:g}/{max_points:g}")
                if level(q) == 1:
                    panel("Kết quả", "ĐÚNG" if score == max_points else "SAI", "feedback")
                else:
                    feedback = norm(result.get("Đánh giá từ Agent") or result.get("Đánh giá từ Agent tham khảo đáp án gợi ý"))
                    panel("Nhận xét của Agent", feedback or "Chưa có nhận xét.", "feedback")
                outside = norm(result.get("Nội dung ngoài đáp án"))
                if outside:
                    panel("Ý ngoài đáp án", outside, "feedback")
            if st.button("Làm bài test mới"):
                del st.session_state[test_key]
                st.rerun()
        st.stop()
    if mode == "Trainer – Điều khiển bài kiểm tra":
        st.subheader("Điều khiển bài kiểm tra")
        recommended = suggested_training_day()
        st.info(f"Agent đề xuất: **Ngày {recommended}** dựa trên lịch trong tab Khóa học và ngày hiện tại.")
        st.caption("Trainer có thể chọn ngày khác tại mục Ngày đào tạo ở thanh bên.")
        schedule_rows = cached_course_values()
        current = next((row for row in schedule_rows[1:] if row and
                        re.search(r"(\d+)", norm(row[0])) and
                        int(re.search(r"(\d+)", norm(row[0])).group(1)) == selected_day), None)
        status = norm(current[4]) if current and len(current) > 4 else "Đóng"
        st.write(f"Ngày đã chọn: **Ngày {selected_day}** · Trạng thái: **{status}**")
        if status == "Mở":
            if st.button("Đóng bài kiểm tra", type="primary"):
                set_exam_open(selected_day, False)
                st.rerun()
        else:
            confirmed = st.checkbox(f"Tôi xác nhận mở bài kiểm tra Ngày {selected_day}",
                                    key=f"confirm_open_day_{selected_day}")
            if st.button(f"Mở bài kiểm tra Ngày {selected_day}", type="primary",
                         disabled=not confirmed):
                other_open = [row for row in schedule_rows[1:] if len(row) > 4 and norm(row[4]) == "Mở"]
                if other_open:
                    st.error("Hãy đóng bài kiểm tra đang mở trước khi mở ngày khác.")
                else:
                    set_exam_open(selected_day, True)
                    st.rerun()
        st.caption("Agent chỉ đề xuất ngày; Trainer xác nhận và quyết định thời điểm mở/đóng. Không tự chấm hoặc gửi báo cáo.")
        st.stop()
    if mode == "Trainer – Chấm bài":
        st.subheader(f"Chấm điểm – Ngày {selected_day}")
        batch_key = f"grading_agent:{selected_day}"
        batch = st.session_state.get(batch_key)
        if batch is None:
            course_start, course_end = course_date_range()
            pending = [r for r in records("Bài kiểm tra")
                       if norm(r.get("Ngày")) == str(selected_day)
                       and norm(r.get("Trạng thái")) == "Chờ chấm"
                       and within_course_period(
                           norm(r.get("Bắt đầu")) or norm(r.get("Cập nhật lúc")),
                           course_start, course_end)]
            st.metric("Bài chờ chấm", len(pending))
            if st.button("Chấm điểm", type="primary", disabled=not pending):
                invalidate_records("Question Bank - L1", "Question Bank - L2/3/4", "Quy tắc đánh giá", "Câu trả lời", "Bài kiểm tra", "Lượt đánh giá")
                st.session_state[batch_key] = {"action": "grade", "queue": pending, "index": 0, "successes": [], "failures": []}
                st.rerun()
        else:
            total, index = len(batch["queue"]), batch["index"]
            st.progress(index / total if total else 1.0, text=f"Đã chấm {index}/{total} bài")
            if index < total:
                item = batch["queue"][index]
                st.info(f"Đang chấm: {norm(item.get('Học viên'))}")
                try:
                    grade_pending_session(item, question_bank(), students)
                    batch["successes"].append(norm(item.get("Học viên")))
                except Exception as exc:
                    batch["failures"].append((norm(item.get("Học viên")), str(exc)))
                batch["index"] += 1
                invalidate_records()
                st.rerun()
            st.success(f"Đã chấm thành công {len(batch['successes'])}/{total} bài.")
            for name, error in batch["failures"]:
                st.error(f"{name}: {error}")
            if st.button("Đóng kết quả chấm"):
                del st.session_state[batch_key]
                invalidate_records()
                st.rerun()
        st.stop()
    if mode == "Trainer – Dashboard":
        st.subheader(f"Dashboard lớp – Ngày {selected_day}")
        st.caption("Dữ liệu báo cáo chỉ được tải và tổng hợp khi Trainer yêu cầu.")
        report_key = f"trainer_report_requested:{selected_day}"
        if st.button("Tạo báo cáo", type="primary"):
            invalidate_records()
            st.session_state[report_key] = True
        if not st.session_state.get(report_key, False):
            st.info("Bấm Tạo báo cáo để xem tổng quan lớp và tiến bộ học viên.")
            st.stop()
        if st.button("Làm mới báo cáo"):
            invalidate_records()
            st.rerun()
        course_start, course_end = course_date_range()
        if not course_start or not course_end:
            st.error("Chưa xác định được khoảng ngày khóa học trong tab Khóa học.")
            st.stop()
        current_sessions = course_session_ids(assignments, course_start, course_end)
        history = [r for r in retry_sheets(lambda: records("Câu trả lời"))
                   if norm(r.get("Session ID")) in current_sessions]
        current_evaluations = [r for r in records("Lượt đánh giá")
                               if norm(r.get("Session ID")) in current_sessions]
        latest = {}
        for r in current_evaluations:
            if norm(r.get("Ngày đánh giá")) == str(selected_day):
                latest[norm(r.get("Họ và tên"))] = r
        rows = list(latest.values())
        completed = [r for r in rows if norm(r.get("Trạng thái")).lower() in ("completed", "hoàn thành")]
        day_assignments = [r for r in assignments
                           if norm(r.get("Session ID")) in current_sessions
                           and norm(r.get("Ngày")) == str(selected_day)]
        submitted_students = {norm(r.get("Học viên")) for r in day_assignments if norm(r.get("Học viên"))}
        pending_assignments = [r for r in day_assignments
                               if norm(r.get("Trạng thái")).casefold() == "chờ chấm"]
        a, b, c, d = st.columns(4)
        a.metric("Đã nộp", len(submitted_students))
        b.metric("Đã chấm", f"{len(completed)}/{len(submitted_students)}")
        c.metric("Chờ chấm", len(pending_assignments))
        scores = [report_number(r.get("Tỷ lệ (%)")) for r in completed]
        d.metric("Điểm TB", f"{sum(scores)/len(scores):.1f}%" if scores else "—")
        for lev in (2, 3, 4):
            vals = [report_number(r.get(f"Điểm Level {lev} (%)")) for r in completed if norm(r.get(f"Điểm Level {lev} (%)"))]
            st.metric(f"Level {lev} TB", f"{sum(vals)/len(vals):.1f}%" if vals else "—")
        if rows:
            st.dataframe([{"Học viên": r.get("Họ và tên"), "Đại lý": r.get("Đại lý"), "Điểm": round(report_number(r.get("Điểm đạt")), 2), "Tỷ lệ (%)": round(report_number(r.get("Tỷ lệ (%)")), 2), "Trạng thái": r.get("Trạng thái"), "Ôn tập": r.get("Nội dung cần ôn tập cho ngày tiếp theo")} for r in rows], hide_index=True, use_container_width=True)
        else:
            if pending_assignments:
                st.info(f"{len(pending_assignments)} bài đã nộp và đang chờ Agent chấm.")
            else:
                st.info("Chưa có bài nộp hoặc lượt đánh giá cho ngày này.")
        st.divider()
        st.subheader("Theo dõi tiến bộ học viên")
        st.caption("So sánh lượt hoàn thành mới nhất của mỗi ngày đào tạo. Chênh lệch điểm tính bằng điểm phần trăm; các ngày có bộ câu hỏi khác nhau nên đây là chỉ báo tham khảo.")
        progress = learner_progress_data(current_evaluations, history, students)
        if progress:
            a, b, c, d = st.columns(4)
            a.metric("Có từ 2 ngày", sum(r["Số ngày đã học"] >= 2 for r in progress))
            b.metric("Tiến bộ", sum(r["Xu hướng"] == "Tiến bộ" for r in progress))
            c.metric("Cần hỗ trợ", sum(r["Xu hướng"] == "Cần hỗ trợ" for r in progress))
            d.metric("Câu đã khắc phục", sum(r["Đã khắc phục"] for r in progress))
            st.dataframe(progress, hide_index=True, use_container_width=True)
            selected_learner = st.selectbox("Xem tiến bộ chi tiết", [r["Học viên"] for r in progress], key="progress_learner")
            detail = next(r for r in progress if r["Học viên"] == selected_learner)
            day_scores = {f"Ngày {d}": detail[f"Ngày {d} (%)"] for d in range(1, 5) if detail[f"Ngày {d} (%)"] is not None}
            if len(day_scores) >= 2:
                st.line_chart([{"Ngày": day, "Điểm (%)": score} for day, score in day_scores.items()], x="Ngày", y="Điểm (%)")
            else:
                st.info("Học viên mới có kết quả một ngày; cần ít nhất hai ngày để xác định xu hướng.")
            st.write(f'**{detail["Xu hướng"]}** · Đã khắc phục: {detail["Đã khắc phục"]} câu · Vẫn chưa khắc phục: {detail["Chưa khắc phục"]} câu')
            if detail["Thay đổi (điểm %)"] is not None:
                st.caption(f'Thay đổi Level 2: {detail["L2 thay đổi"]:+g} điểm % · Level 3: {detail["L3 thay đổi"]:+g} điểm % · Level 4: {detail["L4 thay đổi"]:+g} điểm %')
        else:
            st.info("Chưa có dữ liệu để theo dõi tiến bộ.")
        repeat_alerts = repeated_unpassed_questions(history, students)
        if repeat_alerts:
            st.warning(f"⚠️ {len(repeat_alerts)} câu hỏi của {len({r['Học viên'] for r in repeat_alerts})} học viên vẫn chưa đạt sau ít nhất 2 lần. Xem Trainer – Cảnh báo để biết chi tiết.")
        st.stop()
    if mode == "Trainer – Duyệt & gửi tất cả":
        st.subheader(f"Phê duyệt và gửi kết quả – Ngày {selected_day}")
        ws = sheet("Câu trả lời")
        values = retry_sheets(lambda: ws.get_all_values())
        headers = values[0] if values else []
        required = ("Session ID", "Họ và tên", "Ngày đánh giá", "Trạng thái kiểm duyệt", "Trạng thái gửi email")
        if any(h not in headers for h in required):
            st.error("Thiếu cột bắt buộc trong tab Câu trả lời.")
            st.stop()
        entries = [(i, dict(zip(headers, row + [""] * (len(headers)-len(row)))))
                   for i, row in enumerate(values[1:], 2)]
        course_start, course_end = course_date_range()
        current_sessions = course_session_ids(assignments, course_start, course_end, selected_day)
        daily = [(i, r) for i, r in entries
                 if norm(r.get("Ngày đánh giá")) == str(selected_day)
                 and norm(r.get("Session ID")) in current_sessions]
        # One report per student: use the last recorded session for that day.
        latest_sessions = {}
        for i, r in daily:
            name = norm(r.get("Họ và tên"))
            if name:
                latest_sessions[name] = norm(r.get("Session ID"))
        batches = {}
        for name, sid in latest_sessions.items():
            batches[name] = [(i, r) for i, r in daily
                             if norm(r.get("Họ và tên")) == name and norm(r.get("Session ID")) == sid]
        directory = {norm(r.get("Họ và tên")): norm(r.get("Email") or r.get("E-mail"))
                     for r in students}
        dealer_directory = {norm(r.get("Họ và tên")): norm(r.get("Đại lý")) for r in students}
        smtp_ready = all(st.secrets.get(k) for k in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD"))
        if not smtp_ready:
            st.error("Chưa cấu hình SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD trong Streamlit Secrets.")
        if not batches:
            st.info("Chưa có bài nộp nào cho ngày này.")
            st.stop()
        pending = []
        for name, batch in batches.items():
            email = directory.get(name, "")
            sent = all(norm(r.get("Trạng thái gửi email")).startswith("Đã gửi") for _, r in batch)
            st.write(f"**{name}** · {len(batch)} câu · {email or 'CHƯA CÓ EMAIL'} · {'Đã gửi' if sent else 'Chưa gửi'}")
            if not sent:
                if all(norm(r.get("Trạng thái kiểm duyệt")).startswith("Agent graded") or
                       norm(r.get("Trạng thái kiểm duyệt")).casefold() == "trainer approved"
                       for _, r in batch):
                    pending.append((name, email, batch))
                else:
                    st.warning(f"{name}: Agent chưa chấm đủ các câu.")
        valid_email = lambda value: bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value or ""))

        missing = [name for name, email, _ in pending if not valid_email(email)]
        ready_to_send = [(name, email, batch) for name, email, batch in pending if valid_email(email)]
        if missing:
            st.warning("Thiếu email hợp lệ: " + ", ".join(missing) + ". Các học viên này sẽ được giữ lại để bổ sung email và gửi sau.")
        st.caption(
            f"{len(batches)} học viên đã nộp · {len(pending)} chưa nhận email · "
            f"{len(ready_to_send)} đủ điều kiện gửi · {len(missing)} chờ bổ sung email."
        )
        confirmed = st.checkbox("Tôi đã rà soát nhận xét và xác nhận công bố, gửi email cho các học viên đủ email.")
        if st.button("Approve & gửi kết quả cho học viên đủ email", type="primary",
                     disabled=not (confirmed and smtp_ready and bool(ready_to_send))):
            status_col = headers.index("Trạng thái kiểm duyệt") + 1
            email_col = headers.index("Trạng thái gửi email") + 1
            success = []
            failed = []
            for name, email, batch in ready_to_send:
                rows = [r for _, r in batch]
                try:
                    # Mark approved before sending. Email status is saved only after SMTP succeeds.
                    updates = [{"range": gspread.utils.rowcol_to_a1(i, status_col),
                                "values": [["Trainer approved"]]} for i, _ in batch]
                    retry_sheets(lambda: ws.batch_update(updates, value_input_option="RAW"))
                    send_report_email(email, name, dealer_directory.get(name, ""), selected_day, rows)
                    updates = [{"range": gspread.utils.rowcol_to_a1(i, email_col),
                                "values": [["Đã gửi " + timestamp()]]} for i, _ in batch]
                    retry_sheets(lambda: ws.batch_update(updates, value_input_option="RAW"))
                    success.append(name)
                except Exception as exc:
                    failed.append(f"{name}: {exc}")
            invalidate_records()
            if success:
                st.success(f"Đã gửi thành công cho {len(success)} học viên: " + ", ".join(success))
            if failed:
                st.error("Chưa hoàn tất: " + "; ".join(failed))
                st.warning("Có thể email đã được gửi nhưng trạng thái lưu thất bại. Kiểm tra trước khi thử lại để tránh gửi trùng.")
        st.stop()
    if mode == "Trainer – Cảnh báo":
        st.subheader("Cảnh báo câu hỏi chưa đạt nhiều lần")
        st.caption("Toàn bộ học viên, mọi ngày đánh giá. Cùng Question ID, ít nhất 2 session kết luận Chưa đạt và lần trả lời gần nhất vẫn Chưa đạt.")
        if st.button("Làm mới cảnh báo"):
            invalidate_records()
            st.rerun()
        course_start, course_end = course_date_range()
        current_sessions = course_session_ids(assignments, course_start, course_end)
        history = [r for r in history if norm(r.get("Session ID")) in current_sessions]
        warnings = repeated_unpassed_questions(history, students)
        a, b, c = st.columns(3)
        a.metric("Học viên cần hỗ trợ", len({r["Học viên"] for r in warnings}))
        b.metric("Câu hỏi cảnh báo", len(warnings))
        c.metric("Không đạt ≥3 lần", sum(r["Số lần chưa đạt"] >= 3 for r in warnings))
        if warnings:
            st.warning(f"Có {len(warnings)} trường hợp cần Trainer hỗ trợ ôn tập.")
            student_filter = st.selectbox("Lọc học viên", ["Tất cả"] + sorted({r["Học viên"] for r in warnings}), key="warning_student")
            visible = [r for r in warnings if student_filter == "Tất cả" or r["Học viên"] == student_filter]
            st.dataframe(visible, hide_index=True, use_container_width=True)
        else:
            st.success("Không có câu hỏi nào chưa đạt lặp lại từ 2 lần và vẫn chưa đạt ở lần mới nhất.")
        st.stop()
    for lev, count in LEVEL_COUNTS.items():
        eligible = [q for q in bank.values() if 1 <= day(q) <= selected_day and level(q) == lev and approved(q) and (lev == 1 or norm(q.get("Đáp án tham khảo")))]
        st.write(f"Level {lev}: {len(eligible)} câu hợp lệ / cần {count}")
    st.stop()

if role == "Học viên" and active_window is None:
    st.info("Bài kiểm tra hiện đang đóng. Vui lòng chờ Trainer mở bài.")
    st.stop()

st.subheader(f"Bài đánh giá – Ngày {selected_day}")
key = f"exam_v6:{student}:{selected_day}"
if key not in st.session_state:
    existing = assignment_row(student, selected_day, assignments)
    if existing and norm(existing.get("Trạng thái")) in ("Chờ chấm", "Đã chấm", "Đã phân loại", "Hoàn thành"):
        sid = norm(existing.get("Session ID"))
        order = json.loads(existing.get("Danh sách câu JSON") or "[]")
        completed = {norm(x.get("Question ID")): x for x in history if norm(x.get("Session ID")) == sid}
        st.success("Đã nộp bài kiểm tra. Kết quả sẽ được thông báo sau khi Trainer duyệt.")
        st.stop()
    if not history:
        history = retry_sheets(lambda: records("Câu trả lời"))
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
            st.markdown(f"### Câu {i}/{len(order)}")
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
        try:
            # Re-read only the Trainer's open/close switch; historical date/time
            # formatting must not block a submission.
            current_rows = retry_sheets(lambda: sheet("Khóa học").get_all_values())
            open_days = []
            for course_row in current_rows[1:]:
                day_match = re.search(r"(\d+)", norm(course_row[0])) if course_row else None
                if day_match and len(course_row) > 4 and norm(course_row[4]).casefold() == "mở":
                    open_days.append(int(day_match.group(1)))
            if selected_day not in open_days:
                st.error("Bài kiểm tra đã đóng. Vui lòng liên hệ Trainer trước khi nộp.")
                st.stop()
        except Exception as exc:
            st.error(f"Không xác minh được trạng thái mở bài kiểm tra: {exc}")
            st.stop()
        if any(not responses.get(item["id"], "").strip() for item in order):
            st.warning("Bạn còn câu chưa trả lời. Vui lòng hoàn thành trước khi nộp.")
            st.stop()
        exam["responses"] = responses
        exam["status"] = "raw_saving"
        st.rerun()
    st.stop()

if exam["status"] == "raw_saving":
    with st.spinner("Đang lưu và xác minh câu trả lời gốc..."):
        try:
            save_raw_answers(exam["sid"], student, selected_day, order, bank, exam["responses"])
            queue_submission(exam["sid"], student, selected_day, order)
            exam["status"] = "queued"
            st.rerun()
        except Exception as exc:
            st.error(f"Chưa xác nhận lưu bài: {exc}")
            st.warning("Không đóng trang. Bấm thử lại để xác minh hoặc tiếp tục lưu.")
            if st.button("Thử lưu lại câu trả lời"):
                st.rerun()
    st.stop()

if exam["status"] == "queued":
    st.success("Đã lưu đầy đủ câu trả lời. Bài đang chờ Trainer chủ động chấm và công bố kết quả.")
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
    st.success("Đã hoàn thành bài kiểm tra. Kết quả sẽ được thông báo sau.")
else:
    st.error("Đã chấm bài nhưng Google Sheets chưa xác nhận lưu thành công. Kết quả vẫn được giữ trong phiên hiện tại.")
    st.caption(exam.get("save_error", "Lỗi đồng bộ Google Sheets."))
    if st.button("Thử đồng bộ lại kết quả", type="primary"):
        exam["status"] = "saving"
        st.rerun()


