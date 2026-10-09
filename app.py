"""
PhysicsLab 物理学情分析系统 - Agent 后端
========================================
架构：前端 → Flask Agent（DeepSeek API + SQLite）→ 返回报告
数据库：SQLite（灵活结构，支持扩展字段）
运行：pip install flask flask-cors requests  →  python app.py
访问：http://localhost:5000
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3
import json
import os
import uuid
import base64
import requests
from datetime import datetime
import oss2

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app, resources={r"/*": {"origins": "*"}})
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB 上传限制

# 阿里云函数计算适配：只有 /tmp 目录可写
# 本地开发时用当前目录，云端部署时用 /tmp
if os.environ.get('FC_FUNCTION_NAME'):
    # 阿里云函数计算环境
    BASE_DIR = '/tmp'
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(BASE_DIR, 'physics_lab.db')
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ===== 阿里云 OSS 配置 =====
# 从环境变量读取，本地开发时也可以直接填写
OSS_ACCESS_KEY_ID = os.environ.get('OSS_ACCESS_KEY_ID', '')
OSS_ACCESS_KEY_SECRET = os.environ.get('OSS_ACCESS_KEY_SECRET', '')
OSS_ENDPOINT = os.environ.get('OSS_ENDPOINT', 'oss-cn-hangzhou.aliyuncs.com')
OSS_BUCKET_NAME = os.environ.get('OSS_BUCKET_NAME', '')
OSS_DB_KEY = 'physics_lab.db'  # OSS 中数据库文件的路径

# 初始化 OSS 客户端
oss_bucket = None
if OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET and OSS_BUCKET_NAME:
    try:
        auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
        oss_bucket = oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET_NAME)
        print('[OSS] 阿里云 OSS 客户端初始化成功')
    except Exception as e:
        print(f'[OSS] OSS 初始化失败: {e}')
        oss_bucket = None
else:
    print('[OSS] 未配置 OSS 环境变量，数据库将不会同步到 OSS')

# ===== DeepSeek API 配置 =====
DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
DEEPSEEK_MODEL = 'deepseek-flash'


# ==================== OSS 同步函数 ====================

def sync_db_from_oss():
    """从 OSS 下载数据库文件到本地"""
    if not oss_bucket:
        return False
    try:
        # 检查 OSS 中是否存在数据库文件
        try:
            oss_bucket.get_object_meta(OSS_DB_KEY)
        except oss2.exceptions.NoSuchKey:
            print('[OSS] OSS 中不存在数据库文件，跳过下载')
            return False
        
        # 下载数据库文件
        oss_bucket.get_object_to_file(OSS_DB_KEY, DB_PATH)
        print('[OSS] 从 OSS 下载数据库成功')
        return True
    except Exception as e:
        print(f'[OSS] 从 OSS 下载数据库失败: {e}')
        return False


def sync_db_to_oss():
    """将本地数据库文件上传到 OSS"""
    if not oss_bucket:
        return False
    try:
        oss_bucket.put_object_from_file(OSS_DB_KEY, DB_PATH)
        print('[OSS] 数据库已同步到 OSS')
        return True
    except Exception as e:
        print(f'[OSS] 同步数据库到 OSS 失败: {e}')
        return False


# ==================== DATABASE ====================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    # 如果配置了 OSS，先尝试从 OSS 下载数据库
    if oss_bucket:
        sync_db_from_oss()
    
    conn = get_db()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS students (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            class_name TEXT DEFAULT '',
            school TEXT DEFAULT '',
            grade TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            profile_data TEXT DEFAULT '{}',
            custom_fields TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS exams (
            id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            exam_date TEXT NOT NULL,
            exam_type TEXT NOT NULL DEFAULT '考试',
            score REAL DEFAULT 0,
            total_score REAL DEFAULT 100,
            analysis_data TEXT DEFAULT '{}',
            raw_data TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS exam_reports (
            id TEXT PRIMARY KEY,
            exam_id TEXT NOT NULL UNIQUE,
            prompt TEXT,
            report_text TEXT,
            structured_data TEXT DEFAULT '{}',
            image_paths TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS learning_reports (
            id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            exam_ids TEXT DEFAULT '[]',
            prompt TEXT,
            report_text TEXT,
            structured_data TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS student_notes (
            id TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            note_type TEXT DEFAULT 'general',
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
        )
    ''')

    c.execute('CREATE INDEX IF NOT EXISTS idx_exams_student ON exams(student_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_exams_date ON exams(exam_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_reports_exam ON exam_reports(exam_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_lr_student ON learning_reports(student_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_notes_student ON student_notes(student_id)')

    conn.commit()
    conn.close()
    print('[OK] 数据库初始化完成:', DB_PATH)


# ==================== 辅助函数 ====================

def safe_json_loads(text, default=None):
    if default is None:
        default = {}
    try:
        return json.loads(text) if text else default
    except (json.JSONDecodeError, TypeError):
        return default


def row_to_dict(row, json_fields=None):
    if row is None:
        return None
    d = dict(row)
    if json_fields:
        for f in json_fields:
            d[f] = safe_json_loads(d.get(f))
    return d


def extract_json_from_response(text):
    """从 AI 回复中提取 JSON 块"""
    import re
    match = re.search(r'```json\s*([\s\S]*?)```', text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 尝试直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


def clean_report_text(text):
    """移除 AI 回复中的 JSON 块和自我介绍，保留纯文本报告"""
    import re
    # 移除 JSON 块
    text = re.sub(r'```json\s*[\s\S]*?```', '', text).strip()
    # 移除常见的 AI 自我介绍句式（多行、多模式匹配）
    patterns = [
        # 角色设定类
        r'^[我]*作为[一]*[位个]*[经验资深]*丰富的?的?[高中]?物理[教学]?专家[，,。:.]?\s*',
        r'^我是[一]*[位个]*[经验资深]*丰富的?的?[高中]?物理[教学]?专家[，,。:.]?\s*',
        r'^作为[一]*[位个]*[经验资深]*丰富的?的?[高中]?物理[教学]?专家[，,。:.]?\s*',
        r'^我是一位物理[教学]?专家[，,。:.]?\s*',
        r'^作为物理[教学]?专家[，,。:.]?\s*',
        r'^你是一位[^\n]*专家[^\n]*\n\s*',  # "你是一位..." 这种prompt残留
        r'^请[^\n]*分析[^\n]*\n\s*(?=#+\s)',  # "请仔细分析..." 这种指令残留
        # 问候语类
        r'^你好[！!]?\s*我是[^\n]*专家[^\n]*\n\s*',
        r'^你好[！!]?[^\n]*\n\s*(?=#+\s)',  # "你好！" 开头
        # 分析引导语类
        r'^让我来?来?[^\n]*分析[^\n]*\n\s*',
        r'^让我来?[^\n]*\n\s*(?=#+\s)',  # "让我来..."
        r'^以下是[我]*[^\n]*分析报告?[^\n]*\n\s*(?=#+\s)',
        r'^下面[我]*[^\n]*分析[^\n]*\n\s*(?=#+\s)',
        # 口语化开头
        r'^(好的|当然|没问题|收到|明白)[，,。！!][^\n]*\n\s*',
        r'^(好的|当然|没问题|收到|明白)[，,。！!]?\s*(?=#+\s)',
        # 多行自我介绍（匹配到第一个标题前）
        r'^[^\n]*专家[^\n]*\n[^\n]*\n\s*(?=#+\s)',
        r'^[^\n]*物理[^\n]*\n[^\n]*\n\s*(?=#+\s)',
    ]
    for pat in patterns:
        text = re.sub(pat, '', text, flags=re.MULTILINE)
    # 移除开头可能的空行
    text = re.sub(r'^\s*\n+', '', text)
    return text.strip()


# ==================== DeepSeek AI Agent ====================

def call_deepseek_vision(prompt, image_data_urls):
    """调用 DeepSeek 视觉模型分析试卷图片"""
    content = []
    for data_url in image_data_urls:
        content.append({
            'type': 'image_url',
            'image_url': {'url': data_url}
        })
    content.append({'type': 'text', 'text': prompt})

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {DEEPSEEK_KEY}'
    }
    payload = {
        'model': DEEPSEEK_MODEL,
        'messages': [{'role': 'user', 'content': content}],
        'max_tokens': 8192,
        'temperature': 0.3
    }

    print(f'[Agent] 调用 DeepSeek 视觉API（{len(image_data_urls)}张图片）...')
    resp = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=180)

    if resp.status_code != 200:
        error_msg = resp.text
        print(f'[Agent] API 错误 ({resp.status_code}): {error_msg}')
        raise Exception(f'DeepSeek API 错误 ({resp.status_code}): {error_msg}')

    data = resp.json()
    msg = data['choices'][0]['message']
    result = msg.get('content', '') or msg.get('reasoning_content', '')
    if not result:
        result = msg.get('reasoning_content', '') or '(模型未返回内容)'
    print(f'[Agent] 视觉分析完成，回复长度: {len(result)}')
    return result


def call_deepseek_text(prompt):
    """调用 DeepSeek 文本模型生成学情报告"""
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {DEEPSEEK_KEY}'
    }
    payload = {
        'model': DEEPSEEK_MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 8192,
        'temperature': 0.3
    }

    print(f'[Agent] 调用 DeepSeek 文本API...')
    resp = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=180)

    if resp.status_code != 200:
        error_msg = resp.text
        print(f'[Agent] API 错误 ({resp.status_code}): {error_msg}')
        raise Exception(f'DeepSeek API 错误 ({resp.status_code}): {error_msg}')

    data = resp.json()
    msg = data['choices'][0]['message']
    result = msg.get('content', '') or msg.get('reasoning_content', '')
    if not result:
        result = msg.get('reasoning_content', '') or '(模型未返回内容)'
    print(f'[Agent] 文本分析完成，回复长度: {len(result)}')
    return result


def build_exam_prompt(student_name, exam_type):
    """构建试卷分析提示词"""
    prompt = """请仔细分析这张物理试卷图片，完成以下多维度深度分析。

【重要规则】
- 不要输出任何自我介绍、身份说明（如"作为一位物理专家"、"我是..."等）
- 不要输出寒暄语（如"你好"、"让我来分析"等）
- 不要复述用户的指令
- 直接以"## 试卷整体分析"或类似标题开始输出
- 直接给出分析结果，不要有任何开场白

【重要】首先，请仔细识别试卷上的分数信息：
- 找到试卷上标注的学生得分（通常在试卷顶部或每大题旁边）
- 找到试卷的总分
- 如果图片中有多页，请综合所有页面的信息

请从以下维度进行全面深入分析：

1. **试卷整体分析**
   - 试卷基本信息（科目、年级、考试类型等）
   - 总分与分值分布
   - 题目数量与题型分布（选择题、填空题、实验题、计算题等）
   - 试卷整体难度评估

2. **知识点覆盖分析**
   - 列出试卷涉及的所有物理知识点
   - 各知识点对应的题号和分值
   - 知识点分布是否均衡
   - 重点考查的核心知识点是什么

3. **逐题深度分析**（对每道题进行详细分析）
   - 题目考查的具体知识点
   - 题目难度（易/中/难）
   - 题目类型（概念理解/公式应用/实验分析/综合计算）
   - 解题关键思路和常用方法
   - 常见错误和易错点
   - 学生作答情况（如果可识别）

4. **学生答题情况深度分析**
   - 总体得分情况
   - 做对的题目及正确思路分析
   - 做错的题目及错误原因深度分析
   - 错误类型分类：概念理解错误、公式运用不当、计算错误、审题不清、实验设计能力不足
   - 每类错误的具体表现和根本原因

5. **薄弱知识点深度诊断**
   - 学生掌握薄弱的知识点（明确列出）
   - 每个薄弱知识点的具体表现（哪道题出错）
   - 薄弱原因分析（是概念不清、公式不熟、还是应用能力不足）
   - 知识漏洞之间的关联性和递进关系
   - 这些薄弱点对后续学习的影响

6. **能力维度评估**（1-10分）
   - 物理概念理解能力
   - 公式运用能力
   - 实验分析能力
   - 综合推理能力
   - 数学运算能力
   - 审题与信息提取能力

7. **针对性改进建议**
   - 针对每个薄弱知识点的具体复习建议
   - 推荐练习题类型和难度
   - 学习方法建议（如何避免同类错误）
   - 短期提升策略（1-2周内可执行的计划）
   - 长期学习方向建议

请确保分析专业、具体、有针对性，不要泛泛而谈。

在报告的最后，请单独输出一段JSON数据（用 ```json 包裹），格式如下：
```json
{
  "score": 数字（学生得分，必须从试卷中识别，如确实无法识别填0）,
  "totalScore": 数字（试卷总分，必须从试卷中识别）,
  "questionCount": 数字（题目总数）,
  "correctCount": 数字（答对题数，如无法识别填0）,
  "knowledgePoints": [
    {"name": "知识点名称", "questions": "对应题号", "score": 分值, "mastery": "掌握/基本掌握/薄弱"}
  ],
  "errorTypes": [
    {"type": "错误类型", "count": 次数, "questions": "涉及题号"}
  ],
  "difficultyDistribution": {"easy": 数量, "medium": 数量, "hard": 数量},
  "questionTypes": [
    {"type": "题型", "count": 数量, "score": 分值, "gotRight": 答对数量}
  ],
  "abilityScores": {
    "conceptUnderstanding": 评分1-10,
    "formulaApplication": 评分1-10,
    "experimentAnalysis": 评分1-10,
    "comprehensiveReasoning": 评分1-10,
    "mathComputation": 评分1-10,
    "informationExtraction": 评分1-10
  }
}
```"""

    if student_name:
        prompt += f'\n\n学生姓名：{student_name}'
    if exam_type:
        prompt += f'\n考试类型：{exam_type}'

    return prompt


def build_learning_prompt(student, exams):
    """构建学情分析提示词"""
    exam_data = []
    for e in exams:
        exam_data.append({
            'date': e['exam_date'],
            'type': e['exam_type'],
            'score': e['score'],
            'totalScore': e['total_score'],
            'knowledgePoints': safe_json_loads(e.get('analysis_data', '{}')).get('knowledgePoints', []),
            'errorTypes': safe_json_loads(e.get('analysis_data', '{}')).get('errorTypes', [])
        })

    scores_str = ','.join([str(e['score']) for e in exams])
    labels_str = ','.join([f'"{e["exam_type"]}"' for e in exams])

    return f"""以下是学生 {student['name']} 在指定阶段内的物理考试数据。

【重要规则】
- 不要输出任何自我介绍、身份说明（如"作为一位物理专家"、"我是..."等）
- 不要输出寒语（如"你好"、"让我来分析"等）
- 不要复述用户的指令
- 直接以"## 阶段学习概况"或类似标题开始输出
- 直接给出分析结果，不要有任何开场白

{json.dumps(exam_data, ensure_ascii=False, indent=2)}

学生档案信息：
- 学校：{student.get('school', '未知')}
- 班级：{student.get('class_name', '未知')}
- 年级：{student.get('grade', '未知')}
- 个人特点：{json.dumps(safe_json_loads(student.get('profile_data', '{}')), ensure_ascii=False)}

请根据以上数据，生成一份详细的阶段性学情分析报告。报告应包含以下维度：

1. **阶段学习概况**
   - 考试次数与时间跨度
   - 分数变化趋势描述
   - 整体表现评价

2. **成绩趋势分析**
   - 成绩是上升、下降还是波动
   - 变化幅度分析
   - 关键转折点分析

3. **知识点掌握变化**
   - 各知识点在不同考试中的表现变化
   - 已改善的知识点
   - 仍然薄弱的知识点
   - 新出现的薄弱环节

4. **错题演变分析**
   - 反复出错的知识点
   - 已克服的错误类型
   - 持续存在的错误模式

5. **能力发展评估**
   - 各项能力（概念理解、公式运用、实验分析、综合推理、数学运算、信息提取）的变化趋势
   - 最突出的能力与最需要提升的能力

6. **学习态度与方法评估**
   - 从成绩变化推断的学习态度
   - 学习方法的有效性分析

7. **阶段性改进建议**
   - 下一阶段的学习重点
   - 具体的学习方法和策略建议
   - 推荐练习方向
   - 预期目标设定

请确保分析具有针对性、建设性，并基于数据给出客观评价。

在报告最后，请输出JSON数据（用 ```json 包裹）：
```json
{{
  "scoreTrend": "上升/下降/波动/稳定",
  "scores": [{scores_str}],
  "examLabels": [{labels_str}],
  "improvedTopics": ["已改善的知识点"],
  "weakTopics": ["仍然薄弱的知识点"],
  "recurringErrors": ["反复出错的知识点"],
  "abilityProgress": {{
    "conceptUnderstanding": [各次考试评分],
    "formulaApplication": [各次考试评分],
    "experimentAnalysis": [各次考试评分],
    "comprehensiveReasoning": [各次考试评分],
    "mathComputation": [各次考试评分],
    "informationExtraction": [各次考试评分]
  }},
  "overallAssessment": "综合评级(A/B/C/D)",
  "nextGoals": ["目标1", "目标2"]
}}
```"""


# ==================== 初始化数据库（兼容 gunicorn 启动） ====================
init_db()

# ==================== 静态文件 ====================

@app.route('/')
def serve_index():
    return jsonify({
        'status': 'running',
        'message': 'PhysicsLab API is running!',
        'version': '1.0.0'
    })


# ==================== Agent API: 试卷分析 ====================

@app.route('/api/agent/analyze-exam', methods=['POST'])
def agent_analyze_exam():
    """
    Agent 试卷分析接口
    前端上传：图片文件 + 学生姓名(可选) + 考试类型
    Agent 处理：调用 DeepSeek 视觉API → 解析结果 → 存入数据库 → 返回报告
    """
    student_name = request.form.get('studentName', '').strip()
    exam_type = request.form.get('examType', '').strip()
    manual_score = request.form.get('manualScore', '').strip()
    manual_total = request.form.get('manualTotal', '').strip()
    images = request.files.getlist('images')

    if not images or len(images) == 0:
        return jsonify({'error': '请上传至少一张试卷图片'}), 400

    # 1. 保存图片并转为 base64
    image_data_urls = []
    image_paths = []
    for img in images:
        if img.filename:
            # 保存原图到 uploads 目录
            img_id = str(uuid.uuid4())[:8]
            ext = os.path.splitext(img.filename)[1] or '.png'
            filename = f'{img_id}{ext}'
            filepath = os.path.join(UPLOAD_DIR, filename)
            img.save(filepath)
            image_paths.append(filename)

            # 转为 base64 data URL
            img_bytes = img.read()
            if not img_bytes:
                with open(filepath, 'rb') as f:
                    img_bytes = f.read()
            b64 = base64.b64encode(img_bytes).decode('utf-8')
            mime = img.content_type or 'image/png'
            image_data_urls.append(f'data:{mime};base64,{b64}')

    print(f'[Agent] 收到试卷分析请求：学生={student_name or "无"}, 类型={exam_type or "未指定"}, 图片={len(images)}张')

    # 2. 查找或创建学生
    conn = get_db()
    student_id = None
    if student_name:
        existing = conn.execute('SELECT * FROM students WHERE name = ?', (student_name,)).fetchone()
        if existing:
            student_id = existing['id']
        else:
            student_id = 'stu_' + str(uuid.uuid4())[:8]
            conn.execute(
                'INSERT INTO students (id, name) VALUES (?, ?)',
                (student_id, student_name)
            )
            conn.commit()
            print(f'[Agent] 创建新学生: {student_name} ({student_id})')

    # 3. 调用 DeepSeek 视觉API
    try:
        prompt = build_exam_prompt(student_name, exam_type)
        ai_response = call_deepseek_vision(prompt, image_data_urls)
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

    # 4. 解析 AI 回复
    report_text = clean_report_text(ai_response)
    analysis_data = extract_json_from_response(ai_response)

    # 5. 提取分数（优先级：手动输入 > JSON > 文本提取）
    score = 0
    total_score = 100

    # 优先使用手动输入的成绩
    if manual_score:
        try:
            score = float(manual_score)
            print(f'[Agent] 使用手动输入的成绩: {score}')
        except ValueError:
            pass

    if manual_total:
        try:
            total_score = float(manual_total)
            print(f'[Agent] 使用手动输入的总分: {total_score}')
        except ValueError:
            pass

    # 如果手动输入为空，尝试从 JSON 中获取
    if score == 0:
        score = analysis_data.get('score', 0) or 0

    if total_score == 100:
        total_score = analysis_data.get('totalScore', 100) or 100

    # 如果 JSON 中也没有分数，尝试从报告文本中提取
    if score == 0:
        import re
        # 尝试匹配 "得分：XX" 或 "得分: XX" 或 "XX分" 等模式
        score_match = re.search(r'(?:得分|分数|成绩)[：:\s]*(\d+(?:\.\d+)?)\s*(?:分)?', report_text)
        if score_match:
            score = float(score_match.group(1))
            print(f'[Agent] 从报告文本中提取到分数: {score}')

        # 尝试匹配总分
        total_match = re.search(r'(?:总分|满分)[：:\s]*(\d+(?:\.\d+)?)\s*(?:分)?', report_text)
        if total_match:
            total_score = float(total_match.group(1))
            print(f'[Agent] 从报告文本中提取到总分: {total_score}')

    # 6. 存入数据库
    exam_id = 'exam_' + str(uuid.uuid4())[:8]
    report_id = 'rpt_' + str(uuid.uuid4())[:8]
    exam_date = datetime.now().strftime('%Y-%m-%d')

    if student_id:
        conn.execute(
            '''INSERT INTO exams (id, student_id, exam_date, exam_type, score, total_score, analysis_data)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (exam_id, student_id, exam_date, exam_type or '考试',
             score, total_score, json.dumps(analysis_data, ensure_ascii=False))
        )

        conn.execute(
            '''INSERT INTO exam_reports (id, exam_id, prompt, report_text, structured_data, image_paths)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (report_id, exam_id, prompt, report_text,
             json.dumps(analysis_data, ensure_ascii=False),
             json.dumps(image_paths, ensure_ascii=False))
        )
        conn.commit()
        sync_db_to_oss()  # 同步到 OSS

    conn.close()
    print(f'[Agent] 分析结果已保存，exam_id={exam_id}')

    # 6. 返回结果
    return jsonify({
        'success': True,
        'examId': exam_id,
        'studentId': student_id,
        'reportText': report_text,
        'analysisData': analysis_data,
        'score': score,
        'totalScore': total_score,
        'examType': exam_type or '考试',
        'date': exam_date,
        'imagePaths': image_paths
    })


# ==================== Agent API: 学情分析 ====================

@app.route('/api/agent/generate-learning-report', methods=['POST'])
def agent_generate_learning_report():
    """
    Agent 学情分析接口
    前端发送：studentId + startDate + endDate
    Agent 处理：从DB读取考试数据 → 调用 DeepSeek 文本API → 存入数据库 → 返回报告
    """
    data = request.json
    student_id = data.get('studentId')
    start_date = data.get('startDate', '')
    end_date = data.get('endDate', '')

    if not student_id:
        return jsonify({'error': '请选择学生'}), 400

    conn = get_db()

    # 1. 从数据库读取学生信息和考试数据
    student_row = conn.execute('SELECT * FROM students WHERE id = ?', (student_id,)).fetchone()
    if not student_row:
        conn.close()
        return jsonify({'error': '学生不存在'}), 404

    student = row_to_dict(student_row, ['profile_data', 'custom_fields'])

    # 2. 按日期范围查询考试记录
    query = 'SELECT * FROM exams WHERE student_id = ?'
    params = [student_id]
    if start_date:
        query += ' AND exam_date >= ?'
        params.append(start_date)
    if end_date:
        query += ' AND exam_date <= ?'
        params.append(end_date)
    query += ' ORDER BY exam_date ASC'

    exams = conn.execute(query, params).fetchall()
    exams_list = [row_to_dict(e, ['analysis_data', 'raw_data']) for e in exams]

    if len(exams_list) == 0:
        conn.close()
        return jsonify({'error': '该时间段内没有考试记录'}), 400

    conn.close()
    print(f'[Agent] 生成学情报告：学生={student["name"]}, 考试数={len(exams_list)}, 范围={start_date or "起始"}~{end_date or "至今"}')

    # 3. 调用 DeepSeek 文本API
    try:
        prompt = build_learning_prompt(student, exams_list)
        ai_response = call_deepseek_text(prompt)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # 4. 解析 AI 回复
    report_text = clean_report_text(ai_response)
    analysis_data = extract_json_from_response(ai_response)

    # 5. 存入数据库
    report_id = 'lr_' + str(uuid.uuid4())[:8]
    exam_ids = [e['id'] for e in exams_list]

    conn = get_db()
    conn.execute(
        '''INSERT INTO learning_reports
           (id, student_id, start_date, end_date, exam_ids, prompt, report_text, structured_data)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (report_id, student_id, start_date, end_date,
         json.dumps(exam_ids, ensure_ascii=False),
         prompt, report_text,
         json.dumps(analysis_data, ensure_ascii=False))
    )
    conn.commit()
    sync_db_to_oss()  # 同步到 OSS
    conn.close()

    print(f'[Agent] 学情报告已保存，report_id={report_id}')

    return jsonify({
        'success': True,
        'reportId': report_id,
        'reportText': report_text,
        'analysisData': analysis_data,
        'examCount': len(exams_list),
        'startDate': start_date,
        'endDate': end_date
    })


# ==================== 学生 CRUD API ====================

@app.route('/api/students', methods=['GET'])
def get_students():
    conn = get_db()
    rows = conn.execute('SELECT * FROM students ORDER BY created_at DESC').fetchall()
    result = []
    for row in rows:
        student = row_to_dict(row, ['profile_data', 'custom_fields'])
        exams = conn.execute(
            'SELECT * FROM exams WHERE student_id = ? ORDER BY exam_date DESC',
            (student['id'],)
        ).fetchall()
        student['exams'] = [row_to_dict(e, ['analysis_data', 'raw_data']) for e in exams]
        result.append(student)
    conn.close()
    return jsonify(result)


@app.route('/api/students', methods=['POST'])
def create_student():
    data = request.json
    if not data or not data.get('name', '').strip():
        return jsonify({'error': '学生姓名不能为空'}), 400

    name = data['name'].strip()
    conn = get_db()

    existing = conn.execute('SELECT * FROM students WHERE name = ?', (name,)).fetchone()
    if existing:
        conn.close()
        student = row_to_dict(existing, ['profile_data', 'custom_fields'])
        student['exams'] = []
        return jsonify(student)

    student_id = 'stu_' + str(uuid.uuid4())[:8]
    conn.execute(
        '''INSERT INTO students (id, name, class_name, school, grade, phone, profile_data, custom_fields)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (student_id, name,
         data.get('class_name', data.get('className', '')),
         data.get('school', ''), data.get('grade', ''), data.get('phone', ''),
         json.dumps(data.get('profile_data', {}), ensure_ascii=False),
         json.dumps(data.get('custom_fields', {}), ensure_ascii=False))
    )
    conn.commit()
    sync_db_to_oss()  # 同步到 OSS

    row = conn.execute('SELECT * FROM students WHERE id = ?', (student_id,)).fetchone()
    conn.close()
    student = row_to_dict(row, ['profile_data', 'custom_fields'])
    student['exams'] = []
    return jsonify(student), 201


@app.route('/api/students/<student_id>', methods=['GET'])
def get_student(student_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM students WHERE id = ?', (student_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': '学生不存在'}), 404

    student = row_to_dict(row, ['profile_data', 'custom_fields'])
    exams = conn.execute(
        'SELECT * FROM exams WHERE student_id = ? ORDER BY exam_date ASC',
        (student_id,)
    ).fetchall()
    student['exams'] = [row_to_dict(e, ['analysis_data', 'raw_data']) for e in exams]
    conn.close()
    return jsonify(student)


@app.route('/api/students/<student_id>', methods=['PUT'])
def update_student(student_id):
    conn = get_db()
    existing = conn.execute('SELECT * FROM students WHERE id = ?', (student_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({'error': '学生不存在'}), 404

    data = request.json
    updatable = ['name', 'class_name', 'school', 'grade', 'phone']
    field_map = {'className': 'class_name'}

    updates = {}
    for key in updatable:
        if key in data:
            updates[key] = data[key]
    for camel, snake in field_map.items():
        if camel in data:
            updates[snake] = data[camel]

    if 'profile_data' in data:
        current = safe_json_loads(existing['profile_data'])
        current.update(data['profile_data'])
        updates['profile_data'] = json.dumps(current, ensure_ascii=False)

    if 'custom_fields' in data:
        current = safe_json_loads(existing['custom_fields'])
        current.update(data['custom_fields'])
        updates['custom_fields'] = json.dumps(current, ensure_ascii=False)

    if updates:
        updates['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        set_clause = ', '.join(f'{k} = ?' for k in updates)
        values = list(updates.values()) + [student_id]
        conn.execute(f'UPDATE students SET {set_clause} WHERE id = ?', values)
        conn.commit()
        sync_db_to_oss()  # 同步到 OSS

    row = conn.execute('SELECT * FROM students WHERE id = ?', (student_id,)).fetchone()
    student = row_to_dict(row, ['profile_data', 'custom_fields'])
    exams = conn.execute(
        'SELECT * FROM exams WHERE student_id = ? ORDER BY exam_date DESC', (student_id,)
    ).fetchall()
    student['exams'] = [row_to_dict(e, ['analysis_data', 'raw_data']) for e in exams]
    conn.close()
    return jsonify(student)


@app.route('/api/students/<student_id>', methods=['DELETE'])
def delete_student(student_id):
    conn = get_db()
    conn.execute('DELETE FROM students WHERE id = ?', (student_id,))
    conn.commit()
    sync_db_to_oss()  # 同步到 OSS
    conn.close()
    return jsonify({'success': True, 'message': '学生档案已删除'})


# ==================== 考试/报告查询 API ====================

@app.route('/api/exams/<exam_id>', methods=['GET'])
def get_exam(exam_id):
    conn = get_db()
    exam = conn.execute('SELECT * FROM exams WHERE id = ?', (exam_id,)).fetchone()
    if not exam:
        conn.close()
        return jsonify({'error': '考试记录不存在'}), 404
    result = row_to_dict(exam, ['analysis_data', 'raw_data'])
    report = conn.execute('SELECT * FROM exam_reports WHERE exam_id = ?', (exam_id,)).fetchone()
    result['report'] = row_to_dict(report, ['structured_data', 'image_paths']) if report else None
    conn.close()
    return jsonify(result)


@app.route('/api/exams/<exam_id>', methods=['PUT'])
def update_exam(exam_id):
    conn = get_db()
    exam = conn.execute('SELECT * FROM exams WHERE id = ?', (exam_id,)).fetchone()
    if not exam:
        conn.close()
        return jsonify({'error': '考试记录不存在'}), 404

    data = request.get_json()
    fields = ['exam_date', 'exam_type', 'score', 'total_score']
    updates = []
    values = []
    for field in fields:
        if field in data:
            updates.append(f'{field} = ?')
            values.append(data[field])

    if updates:
        values.append(exam_id)
        conn.execute(f"UPDATE exams SET {', '.join(updates)} WHERE id = ?", values)
        conn.commit()
        sync_db_to_oss()  # 同步到 OSS

    updated_exam = conn.execute('SELECT * FROM exams WHERE id = ?', (exam_id,)).fetchone()
    conn.close()
    return jsonify(row_to_dict(updated_exam, ['analysis_data', 'raw_data']))


@app.route('/api/exams/<exam_id>', methods=['DELETE'])
def delete_exam(exam_id):
    conn = get_db()
    conn.execute('DELETE FROM exam_reports WHERE exam_id = ?', (exam_id,))
    conn.execute('DELETE FROM exams WHERE id = ?', (exam_id,))
    conn.commit()
    sync_db_to_oss()  # 同步到 OSS
    conn.close()
    return jsonify({'success': True, 'message': '考试记录已删除'})


@app.route('/api/students/<student_id>/learning-reports', methods=['GET'])
def get_learning_reports(student_id):
    conn = get_db()
    reports = conn.execute(
        'SELECT * FROM learning_reports WHERE student_id = ? ORDER BY created_at DESC',
        (student_id,)
    ).fetchall()
    conn.close()
    return jsonify([row_to_dict(r, ['exam_ids', 'structured_data']) for r in reports])


# ==================== 学生备注 API ====================

@app.route('/api/students/<student_id>/notes', methods=['GET'])
def get_notes(student_id):
    conn = get_db()
    notes = conn.execute(
        'SELECT * FROM student_notes WHERE student_id = ? ORDER BY created_at DESC',
        (student_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(n) for n in notes])


@app.route('/api/students/<student_id>/notes', methods=['POST'])
def add_note(student_id):
    data = request.json
    conn = get_db()
    note_id = 'note_' + str(uuid.uuid4())[:8]
    conn.execute(
        'INSERT INTO student_notes (id, student_id, note_type, content) VALUES (?, ?, ?, ?)',
        (note_id, student_id, data.get('note_type', 'general'), data.get('content', ''))
    )
    conn.commit()
    sync_db_to_oss()  # 同步到 OSS
    row = conn.execute('SELECT * FROM student_notes WHERE id = ?', (note_id,)).fetchone()
    conn.close()
    return jsonify(dict(row)), 201


@app.route('/api/notes/<note_id>', methods=['DELETE'])
def delete_note(note_id):
    conn = get_db()
    conn.execute('DELETE FROM student_notes WHERE id = ?', (note_id,))
    conn.commit()
    sync_db_to_oss()  # 同步到 OSS
    conn.close()
    return jsonify({'success': True})


# ==================== 统计 API ====================

@app.route('/api/stats', methods=['GET'])
def get_stats():
    conn = get_db()
    student_count = conn.execute('SELECT COUNT(*) as c FROM students').fetchone()['c']
    exam_count = conn.execute('SELECT COUNT(*) as c FROM exams').fetchone()['c']
    avg_row = conn.execute('SELECT AVG(score) as a FROM exams').fetchone()
    avg_score = round(avg_row['a'], 1) if avg_row['a'] else 0
    conn.close()
    return jsonify({'studentCount': student_count, 'examCount': exam_count, 'avgScore': avg_score})


# 模块加载时初始化数据库（兼容 gunicorn 和直接运行）
init_db()


# ==================== 启动 ====================

if __name__ == '__main__':
    # 阿里云函数计算要求监听 9000 端口
    port = int(os.environ.get('PORT', 9000))
    print('=' * 55)
    print('  PhysicsLab 物理学情分析系统 - Agent 后端')
    print(f'  服务地址: http://0.0.0.0:{port}')
    print('  AI 模型:  deepseek-flash')
    print('  数据库:   SQLite →', DB_PATH)
    print('=' * 55)
    
    # 使用 waitress 生产级服务器（兼容阿里云函数计算）
    from waitress import serve
    serve(app, host='0.0.0.0', port=port)
