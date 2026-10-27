"""FA Knowledge Coach – pilot app backed by the trainer's Google Sheets."""
import json
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

st.set_page_config(page_title="FA Knowledge Coach", page_icon="🚙", layout="centered")
st.title("FA Knowledge Coach")
st.caption("Ford Academy Vietnam · Pilot Assessment")


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


@st.cache_resource(show_spinner=False)
def workbook():
    info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def records(tab):
    return workbook().worksheet(tab).get_all_records(default_blank="")


def approved_questions():
    # Read live on every rerun: Trainer approvals become effective without redeploying.
    bank = records("Question Bank")
    l1 = records("Question Bank - L1")
    main = {str(x.get("Question ID", "")).strip(): x for x in bank}
    output, seen = [], set()
    for row in l1:
        qid = str(row.get("Question ID", "")).strip()
        if not qid or qid in seen:
            continue
        seen.add(qid)
        master = main.get(qid)
        # For shared IDs, the master bank controls approval.
        approval = (master or row).get("Trạng thái duyệt", "")
        if str(approval).strip().casefold() not in APPROVED:
            continue
        choices = {k: str(row.get(k, "")).strip() for k in "ABCD"}
        answer = str(row.get("Đáp án đề gốc", "")).upper().strip()
        selected = re.findall(r"[A-D]", answer)
        if not any(choices.values()) or not selected or any(not choices.get(k) for k in selected):
            continue  # Open-ended L1 rows are not MCQs.
        row = dict(row)
        row["_answer_keys"] = sorted(set(selected))
        row["_choices"] = {k: v for k, v in choices.items() if v}
        output.append(row)
    return output


def learners():
    return [x for x in records("Học viên") if x.get("Họ và tên")]


def append_answer(row):
    workbook().worksheet("Câu trả lời").append_row(row, value_input_option="RAW")


def append_session(row):
    workbook().worksheet("Lượt đánh giá").append_row(row, value_input_option="RAW")


def grade_ai(question, answer):
    if not st.secrets.get("OPENAI_API_KEY"):
        raise RuntimeError("Chưa cấu hình OPENAI_API_KEY trong Streamlit Secrets")
    client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=st.secrets.get("OPENAI_MODEL", "gpt-4.1-mini"),
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": (
                "Bạn là giám khảo Ford Academy. Chỉ chấm theo đáp án tham khảo và rubric do Trainer cung cấp. "
                "Nếu đáp án/rubric không đủ để xác định đúng sai, đánh dấu needs_review=true, "
                "không suy đoán. Trả JSON: score (0..10), feedback, needs_review (boolean)."
            )},
            {"role": "user", "content": json.dumps({
                "question": question.get("Câu hỏi", ""),
                "reference": question.get("Đáp án tham khảo", ""),
                "rubric": question.get("Rubric", ""),
                "student_answer": answer,
            }, ensure_ascii=False)},
        ],
    )
    result = json.loads(response.choices[0].message.content)
    score = max(0, min(10, float(result.get("score", 0))))
    return score, str(result.get("feedback", "")), bool(result.get("needs_review", True))


try:
    people = learners()
    questions = approved_questions()
    master = records("Question Bank")
except Exception as exc:
    st.error("Không kết nối được Google Sheets. Kiểm tra Streamlit Secrets và quyền chia sẻ Sheet.")
    st.exception(exc)
    st.stop()

names = [x["Họ và tên"] for x in people]
if not names:
    st.warning("Chưa có học viên trong tab Học viên.")
    st.stop()

with st.sidebar:
    st.header("Pilot")
    st.metric("L1 trắc nghiệm đã duyệt và hợp lệ", len(questions))
    st.caption("Câu L1 tự luận chưa có A–D sẽ không xuất hiện trong bài trắc nghiệm.")
    mode = st.radio("Chế độ", ["Level 1 · Trắc nghiệm", "Level 2–4 · AI chấm thử"])

student = st.selectbox("Học viên", names)
learner = next(x for x in people if x["Họ và tên"] == student)

if mode.startswith("Level 1"):
    if not questions:
        st.warning("Chưa có câu L1 trắc nghiệm đủ A–D và Approved. Hãy duyệt câu MCQ trong tab Question Bank - L1.")
        st.stop()
    st.info("Chọn đáp án → hiển thị đúng/sai ngay → lưu vào Google Sheets. Không sửa được đáp án đã nộp.")
    if "session" not in st.session_state:
        st.session_state.session = None
    if st.session_state.session is None:
        n = st.number_input("Số câu kiểm thử", min_value=1, max_value=min(10, len(questions)), value=min(5, len(questions)))
        if st.button("Bắt đầu", type="primary"):
            import random
            chosen = random.sample(questions, int(n))
            st.session_state.session = {"id": str(uuid.uuid4()), "student": student, "questions": chosen,
                                        "start": now(), "index": 0, "responses": [], "feedback": None}
            st.rerun()
    else:
        s = st.session_state.session
        if student != s["student"]:
            st.warning("Bài đang làm thuộc học viên khác. Hoàn tất hoặc làm mới trước khi đổi tên.")
            st.stop()
        idx = s["index"]
        if idx >= len(s["questions"]):
            score = sum(x["score"] for x in s["responses"])
            st.success(f"Hoàn thành: {score}/{len(s['questions']) * 10} điểm")
            if st.button("Bắt đầu bài mới"):
                st.session_state.session = None
                st.rerun()
            st.stop()
        q = s["questions"][idx]
        st.progress(idx / len(s["questions"]))
        st.subheader(f"Câu {idx+1}/{len(s['questions'])}")
        st.write(q["Câu hỏi"])
        keys = list(q["_choices"])
        multiple = len(q["_answer_keys"]) > 1
        if s["feedback"] is None:
            with st.form(f"answer-{s['id']}-{idx}"):
                if multiple:
                    st.caption("Chọn tất cả đáp án đúng")
                    picks = [k for k in keys if st.checkbox(f"{k}. {q['_choices'][k]}", key=f"{s['id']}-{idx}-{k}")]
                else:
                    choice = st.radio("Chọn một đáp án", keys, format_func=lambda k: f"{k}. {q['_choices'][k]}", index=None)
                    picks = [choice] if choice else []
                submit = st.form_submit_button("Xác nhận đáp án", type="primary")
            if submit:
                if not picks:
                    st.warning("Hãy chọn ít nhất một đáp án.")
                else:
                    correct = sorted(picks) == q["_answer_keys"]
                    score = 10 if correct else 0
                    timestamp = now()
                    try:
                        append_answer([
                            s["id"], student, q.get("Ngày nội dung", ""), q["Question ID"],
                            q.get("Ngày nội dung", ""), q.get("Module", ""), 1, q["Câu hỏi"],
                            ",".join(sorted(picks)), ",".join(q["_answer_keys"]),
                            "Chấm tự động từ đáp án Trainer đã duyệt", score, 10,
                            "Đúng" if correct else "Sai", "" if correct else q.get("Đáp án tham khảo", ""),
                            q.get("File nguồn", ""), timestamp, "Auto graded · Pilot",
                        ])
                    except Exception as exc:
                        st.error(f"Chưa lưu được đáp án. Vui lòng thử lại: {exc}")
                        st.stop()
                    s["responses"].append({"id": q["Question ID"], "score": score})
                    s["feedback"] = {"correct": correct, "answer": q["_answer_keys"]}
                    st.rerun()
        else:
            f = s["feedback"]
            (st.success if f["correct"] else st.error)("Chính xác! +10 điểm" if f["correct"] else "Chưa chính xác. 0 điểm")
            st.write("Đáp án:", ", ".join(f"{k}. {q['_choices'][k]}" for k in f["answer"]))
            if q.get("Đáp án tham khảo"):
                st.caption("Giải thích/đáp án tham khảo: " + str(q["Đáp án tham khảo"]))
            if st.button("Câu tiếp theo" if idx + 1 < len(s["questions"]) else "Hoàn tất bài", type="primary"):
                if idx + 1 == len(s["questions"]):
                    score = sum(x["score"] for x in s["responses"])
                    try:
                        append_session([s["id"], student, learner.get("Đại lý", ""), "Pilot L1", 1,
                                        s["start"], now(), len(s["questions"]), score,
                                        len(s["questions"]) * 10, round(score / (len(s["questions"]) * 10) * 100, 2),
                                        round(score / (len(s["questions"]) * 10) * 100, 2), "", "", "", "", "", "Completed"])
                    except Exception as exc:
                        st.error(f"Chưa lưu được tổng kết: {exc}")
                        st.stop()
                s["index"] += 1
                s["feedback"] = None
                st.rerun()
else:
    ai_q = [x for x in master if str(x.get("Trạng thái duyệt", "")).strip().casefold() in APPROVED
            and str(x.get("Level", "")) in {"2", "3", "4"}]
    if not ai_q:
        st.warning("Chưa có câu Level 2–4 Approved.")
        st.stop()
    qids = {x["Question ID"]: x for x in ai_q}
    qid = st.selectbox("Câu hỏi AI", list(qids), format_func=lambda x: f"{x} · {qids[x]['Câu hỏi'][:70]}")
    q = qids[qid]
    st.write(q["Câu hỏi"])
    answer = st.text_area("Câu trả lời học viên", height=160)
    if st.button("Gửi AI chấm thử", disabled=not answer.strip(), type="primary"):
        try:
            score, feedback, needs_review = grade_ai(q, answer)
            sid = str(uuid.uuid4())
            append_answer([sid, student, q.get("Ngày nội dung", ""), qid, q.get("Ngày nội dung", ""),
                           q.get("Module", ""), q.get("Level", ""), q["Câu hỏi"], answer,
                           q.get("Đáp án tham khảo", ""), feedback, score, 10,
                           "Cần Trainer kiểm duyệt" if needs_review else "AI đánh giá tạm thời",
                           "", q.get("Trang nguồn", ""), now(), "Pending Trainer review"])
            st.metric("Điểm AI tạm chấm", f"{score:g}/10")
            st.write(feedback)
            st.warning("Điểm AI chỉ là kết quả thử nghiệm, cần Trainer kiểm duyệt trước khi công nhận.")
        except Exception as exc:
            st.error(f"Không gọi/lưu được kết quả API: {exc}")
