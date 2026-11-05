import ast, copy, json, re, types, unittest
from pathlib import Path
source=(Path(__file__).resolve().parents[1] / 'app.py').read_text();tree=ast.parse(source)
names={'append_data_rows','save_raw_answers','queue_submission','grade_pending_session','grade_submission','commit_results','learning_journey','norm','level','report_number'}
body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
def a1(row,col):
 s=''
 while col:col,m=divmod(col-1,26);s=chr(65+m)+s
 return s+str(row)
class Book:
 def __init__(self):self.sheets={};self.fail=None
 def batch_update(self,payload):
  for req in payload['requests']:
   a=req['appendCells'];ws=next(w for w in self.sheets.values() if w.id==a['sheetId'])
   rows=[[c['userEnteredValue']['stringValue'] for c in r['values']] for r in a['rows']]
   ws.rows.extend(rows)
   if self.fail==ws.name:self.fail=None;raise TimeoutError('committed before connection lost')
class WS:
 def __init__(self,book,name,headers):self.spreadsheet=book;self.name=name;self.id=len(book.sheets)+1;self.rows=[headers];book.sheets[name]=self
 def get_all_values(self,**kw):return copy.deepcopy(self.rows)
 def get_all_records(self,**kw):return [dict(zip(self.rows[0],r)) for r in self.rows[1:]]
 def batch_update(self,updates,**kw):
  for u in updates:
   m=re.match(r'([A-Z]+)(\d+)',u['range']);col=0
   for c in m[1]:col=26*col+ord(c)-64
   row=self.rows[int(m[2])-1];vals=[v if v is not None else '' for v in u['values'][0]];row.extend(['']*max(0,col-1+len(vals)-len(row)));row[col-1:col-1+len(vals)]=vals
class Records:
 def clear(self):pass
class TestFlow(unittest.TestCase):
 def setUp(self):
  self.book=Book();self.headers=['Session ID','Họ và tên','Ngày đánh giá','Question ID','Ngày khóa học','Module','Level','Câu hỏi','Nội dung trả lời học viên','Đáp án chuẩn đã duyệt','Đánh giá từ Agent','Điểm đạt','Điểm tối đa','Kết luận','Kiến thức cần ôn tập','Nguồn đối chiếu','Thời gian trả lời','Trạng thái kiểm duyệt','Trạng thái gửi email']
  self.raw=WS(self.book,'Câu trả lời',self.headers);self.raw.rows.append(['old','Other','1','old-q','','M','1','Old question','Old answer','Old reference','Old feedback',1,1,'Đạt','','','time','Trainer approved','sent'])
  self.assign=WS(self.book,'Bài kiểm tra',['Session ID','Học viên','Ngày','Trạng thái','Bắt đầu','Kết thúc','Danh sách câu JSON','Số câu mới','Số câu ôn','Điểm mới','Điểm tối đa','Cập nhật lúc'])
  self.summary=WS(self.book,'Lượt đánh giá',['Session ID','Họ và tên','Điểm đạt','Trạng thái'])
  self.calls=[]
  def agent(q,a,m):self.calls.append((q['Question ID'],a));return 0,{'feedback':'Needs work','missing_points':['missing']}
  self.ns={'re':re,'json':json,'sheet':lambda name:self.book.sheets[name],'retry_sheets':lambda f:f(),'records':Records(),'POINTS':{1:1,2:2,3:4,4:6},'timestamp':lambda:'now','grade_open':agent,'gspread':types.SimpleNamespace(utils=types.SimpleNamespace(rowcol_to_a1=a1)),'ASSIGN_HEADERS':self.assign.rows[0]}
  exec(compile(ast.Module(body=body,type_ignores=[]),'app.py','exec'),self.ns)
  self.order=[{'id':'q1','review':False},{'id':'q2','review':False}];self.bank={'q1':{'Question ID':'q1','Level':1,'Câu hỏi':'MCQ','_keys':['A'],'Đáp án đề gốc':'A'},'q2':{'Question ID':'q2','Level':2,'Câu hỏi':'Open','Đáp án tham khảo':'Reference'}};self.responses={'q1':'A','q2':'  Tiếng Việt\nline 2  '};self.args=('sid','Student',4,self.order,self.bank,self.responses)
 def save(self):self.ns['save_raw_answers'](*self.args);self.ns['queue_submission']('sid','Student',4,self.order)
 def grade(self):return self.ns['grade_pending_session'](dict(zip(self.assign.rows[0],self.assign.rows[-1])),self.bank,[])
 def test_end_to_end(self):
  old=copy.deepcopy(self.raw.rows);self.save();self.save();self.assertEqual(self.calls,[]);self.assertEqual(self.raw.rows[:2],old);self.assertEqual(len(self.raw.rows),4);self.assertEqual(len(self.assign.rows),2);before=copy.deepcopy([r[:9] for r in self.raw.rows]);self.grade();self.assertEqual(self.calls,[('q2',self.responses['q2'])]);self.assertEqual([r[:9] for r in self.raw.rows],before);self.assertEqual(self.raw.rows[3][11],0);self.assertEqual(self.assign.rows[1][3],'Hoàn thành');self.assertEqual(len(self.summary.rows),2)
 def test_append_timeout_no_duplicate(self):
  self.book.fail='Câu trả lời'
  with self.assertRaises(TimeoutError):self.save()
  self.save();self.assertEqual(len(self.raw.rows),4)
 def test_summary_timeout_keeps_pending(self):
  self.save();self.book.fail='Lượt đánh giá'
  with self.assertRaises(TimeoutError):self.grade()
  self.assertEqual(self.assign.rows[1][3],'Chờ chấm');calls=list(self.calls);self.grade();self.assertEqual(self.calls,calls);self.assertEqual(len(self.summary.rows),2);self.assertEqual(self.assign.rows[1][3],'Hoàn thành')
 def test_agent_failure_keeps_raw(self):
  self.save();before=copy.deepcopy(self.raw.rows)
  def fail(*args):raise RuntimeError('AI unavailable')
  self.ns['grade_open']=fail
  with self.assertRaises(RuntimeError):self.grade()
  self.assertEqual(self.raw.rows,before);self.assertEqual(self.assign.rows[1][3],'Chờ chấm')
 def test_missing_raw_stops_before_ai(self):
  self.save();self.raw.rows.pop()
  with self.assertRaises(RuntimeError):self.grade()
  self.assertEqual(self.calls,[])
 def test_changed_answer_refuses_overwrite(self):
  self.save();self.responses['q2']='changed'
  with self.assertRaises(RuntimeError):self.ns['save_raw_answers'](*self.args)
 def test_duplicate_raw_stops(self):
  self.save();self.raw.rows.append(copy.deepcopy(self.raw.rows[-1]))
  with self.assertRaises(RuntimeError):self.grade()
if __name__=='__main__':unittest.main(verbosity=2)
