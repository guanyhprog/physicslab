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
    """构建试卷分析提示词 - 基于教学分析专业框架"""
    prompt = """请仔细分析这张物理试卷图片，完成以下多维度深度分析。

【输出格式要求 - 必须严格遵守】
- 直接输出 Markdown 格式的分析报告
- 不要输出寒暄语（如"你好"、"让我来分析"等）
- 不要输出任何解释性前言或后记
- 直接以"## 核心发现"开始输出（2-3条关键发现作为执行摘要）
- 每条结论必须形成闭环：**现象 + 关键数字 + 影响对象 + 建议动作**
- 直接给出分析结果，不要有任何开场白

请从以下维度进行全面深入分析：

## 一、核心发现（执行摘要）
- 提炼 2-3 条最关键的发现
- 每条格式示例："力学模块掌握薄弱（得分率仅 42%），建议优先复盘牛顿定律应用并安排专项练习"

## 二、试卷整体分析
- 试卷整体难度评估（偏易/适中/偏难）
- 题型分布与分值结构（选择题、填空题、实验题、计算题各占比例）
- 知识点覆盖范围与分布
- 试卷结构合理性评价

## 三、逐题深度诊断（对每道题进行详细分析）

对每道题输出以下结构化信息：
- **考查知识点**：具体到哪个概念/定律
- **能力要求**：概念理解/公式应用/实验分析/综合计算/信息提取
- **难度评估**：易(P>0.7)/中(0.4≤P≤0.7)/难(P<0.4)
- **解题关键思路**：简明扼要的解题方法
- **常见错误类型**：
  * 概念理解错误（如混淆矢量标量）
  * 公式运用不当（如适用条件不满足）
  * 计算过程错误（如单位换算、代数运算）
  * 审题失误（如忽略关键条件）
  * 方法不当（如解题路径选择低效）
- **区分度判断**：该题能否有效区分不同水平学生（优/良/可/差）

## 四、知识点掌握分析
- 按知识点聚合分析：
  * 涉及的知识点列表
  * 各知识点的分值占比
  * 各知识点的得分率估计
  * 掌握程度评级（优秀/良好/达标/薄弱）
- 知识点间的关联与综合考查分析
- 薄弱知识点排序（按得分率从低到高）

## 五、能力维度评估
对以下各项能力进行评估（给出得分率估计和等级）：
- **概念理解**：对物理概念、定律的理解深度
- **公式运用**：公式选择、变形、代入的准确性
- **实验分析**：实验设计、数据处理、误差分析能力
- **综合推理**：多知识点综合、逻辑推理能力
- **数学运算**：计算准确性、单位换算、图像分析
- **信息提取**：从题干/图表中提取关键信息的能力

## 六、错因归类与诊断
将所有错题按错误原因归类：
1. **知识性错误**：概念不清、定律理解偏差
2. **方法性错误**：解题策略不当、思路选择错误
3. **习惯性错误**：审题粗心、计算失误、书写不规范
4. **能力性错误**：综合分析能力不足、信息提取不全

## 七、针对性改进建议
- **短期（1-2周）**：针对最薄弱的 2-3 个知识点，给出具体复习方法和练习建议
- **中期（1个月）**：能力提升方向，推荐练习题型和学习策略
- **长期**：学习习惯和方法的改进建议

【专业标准参考】
- 难度 P = 平均得分/满分：P>0.7 偏易，0.4-0.7 适中，<0.3 偏难
- 区分度 D：≥0.4 优，0.3-0.4 良，0.2-0.3 可，<0.2 差，负值异常
- 结论表达：现象 + 关键数字 + 影响对象 + 建议动作

请确保分析专业、具体、有针对性，不要泛泛而谈。

在报告的最后，请单独输出一段JSON数据（用 ```json 包裹），格式如下：
```json
{
  "score": 数字（学生得分，必须从试卷中识别，如确实无法识别填0）,
  "totalScore": 数字（试卷总分，必须从试卷中识别）,
  "questionCount": 数字（题目总数）,
  "correctCount": 数字（答对题数，如无法识别填0）,
  "knowledgePoints": [
    {"name": "知识点名称", "questions": "对应题号", "score": 分值, "scoreRate": 得分率0-1, "mastery": "优秀/良好/达标/薄弱"}
  ],
  "errorTypes": [
    {"type": "错误类型（知识性/方法性/习惯性/能力性）", "count": 次数, "questions": "涉及题号"}
  ],
  "difficultyDistribution": {"easy": 数量, "medium": 数量, "hard": 数量},
  "questionTypes": [
    {"type": "题型", "count": 数量, "score": 分值, "gotRight": 答对数量}
  ],
  "abilityScores": {
    "conceptUnderstanding": 得分率0-1,
    "formulaApplication": 得分率0-1,
    "experimentAnalysis": 得分率0-1,
    "comprehensiveReasoning": 得分率0-1,
    "mathComputation": 得分率0-1,
    "informationExtraction": 得分率0-1
  },
  "questionDiagnosis": [
    {"questionNo": "题号", "knowledgePoint": "知识点", "difficulty": "P值0-1", "discrimination": "D值0-1", "abilityType": "能力类型", "commonErrors": ["错误类型"]}
  ]
}
```
"""
    if student_name:
        prompt += f'\n\n学生姓名：{student_name}'
    if exam_type:
        prompt += f'\n考试类型：{exam_type}'
    return prompt


def build_learning_prompt(student, exams):
    """构建学情分析提示词 - 基于教学分析专业框架"""
    exam_data = []
    for e in exams:
        analysis_data = safe_json_loads(e.get('analysis_data', '{}'))
        exam_data.append({
            'date': e['exam_date'],
            'type': e['exam_type'],
            'score': e['score'],
            'totalScore': e['total_score'],
            'scoreRate': round(e['score'] / e['total_score'], 3) if e['total_score'] > 0 else 0,
            'knowledgePoints': analysis_data.get('knowledgePoints', []),
            'errorTypes': analysis_data.get('errorTypes', []),
            'abilityScores': analysis_data.get('abilityScores', {}),
            'questionDiagnosis': analysis_data.get('questionDiagnosis', [])
        })

    scores_str = ','.join([str(e['score']) for e in exams])
    labels_str = ','.join([f'"{e["exam_type"]}"' for e in exams])

    # 计算统计指标
    if exams:
        scores = [e['score'] for e in exams]
        avg_score = sum(scores) / len(scores)
        max_score = max(scores)
        min_score = min(scores)
        score_range = max_score - min_score
    else:
        avg_score = max_score = min_score = score_range = 0

    return f"""以下是学生 {student['name']} 在指定阶段内的物理考试数据。

【输出格式要求 - 必须严格遵守】
- 直接输出 Markdown 格式的分析报告
- 不要输出寒暄语（如"你好"、"让我来分析"等）
- 不要输出任何解释性前言或后记
- 直接以"## 核心发现"开始输出（2-3条关键发现作为执行摘要）
- 每条结论必须形成闭环：**现象 + 关键数字 + 影响对象 + 建议动作**
- 直接给出分析结果，不要有任何开场白

【考试数据】
{json.dumps(exam_data, ensure_ascii=False, indent=2)}

【学生档案信息】
- 学校：{student.get('school', '未知')}
- 班级：{student.get('class_name', '未知')}
- 年级：{student.get('grade', '未知')}
- 个人特点：{json.dumps(safe_json_loads(student.get('profile_data', '{}')), ensure_ascii=False)}

【统计指标】
- 考试次数：{len(exams)}
- 平均分：{avg_score:.1f}
- 最高分：{max_score}
- 最低分：{min_score}
- 波动幅度：{score_range}

请根据以上数据，生成一份详细的阶段性学情分析报告。报告应包含以下维度：

## 一、核心发现（执行摘要）
- 提炼 2-3 条最关键的发现
- 每条格式示例："力学模块从 45% 提升至 72%，进步显著，但电学模块仍薄弱（52%），建议加强电路分析专项训练"

## 二、阶段学习概况
- 考试次数与时间跨度
- 整体表现评价（结合平均分、波动幅度）
- 学习状态判断（稳定进步/波动/下滑）

## 三、成绩趋势分析
- 成绩变化趋势（上升/下降/波动/稳定）
- 变化幅度与关键转折点
- 趋势背后的可能原因分析

## 四、知识点掌握演变
- 按知识点追踪：哪些已改善、哪些仍薄弱、哪些新出现
- 知识点掌握度排序（从低到高）
- 知识漏洞的关联性分析

## 五、错题演变分析
- 反复出错的知识点（顽固性问题）
- 已克服的错误类型（进步点）
- 持续存在的错误模式
- 错误类型分布（知识性/方法性/习惯性/能力性）

## 六、能力发展评估
对以下各项能力的变化趋势进行评估：
- **概念理解**：对物理概念、定律的理解深度变化
- **公式运用**：公式选择、变形、代入的准确性变化
- **实验分析**：实验设计、数据处理能力变化
- **综合推理**：多知识点综合、逻辑推理能力变化
- **数学运算**：计算准确性、单位换算能力变化
- **信息提取**：从题干/图表提取关键信息的能力变化

## 七、分层诊断与定位
根据当前表现，判断学生所处层次：
- **优秀**（得分率≥85%）：拔尖方向建议
- **良好**（75%-85%）：突破瓶颈方向
- **达标**（60%-75%）：巩固提升方向
- **临界**（50%-60%）：重点帮扶方向
- **预警**（<50%）：基础补救方向

## 八、针对性改进建议
- **短期（1-2周）**：针对最薄弱的 2-3 个知识点，给出具体复习方法
- **中期（1个月）**：能力提升方向，推荐练习题型和学习策略
- **长期**：学习习惯和方法的改进建议
- **预期目标**：设定合理可达成的阶段性目标

【专业标准参考】
- 结论表达：现象 + 关键数字 + 影响对象 + 建议动作
- 解释性假设和建议标注"供参考，请结合教学实际判断"

请确保分析具有针对性、建设性，并基于数据给出客观评价。

在报告最后，请输出JSON数据（用 ```json 包裹）：
```json
{{
  "scoreTrend": "上升/下降/波动/稳定",
  "scores": [{scores_str}],
  "examLabels": [{labels_str}],
  "statistics": {{
    "average": {avg_score:.1f},
    "max": {max_score},
    "min": {min_score},
    "range": {score_range},
    "stdDev": 标准差
  }},
  "tierLevel": "优秀/良好/达标/临界/预警",
  "improvedTopics": ["已改善的知识点"],
  "weakTopics": ["仍然薄弱的知识点"],
  "recurringErrors": ["反复出错的知识点"],
  "errorDistribution": {{
    "knowledge": 知识性错误占比,
    "method": 方法性错误占比,
    "habit": 习惯性错误占比,
    "ability": 能力性错误占比
  }},
  "abilityProgress": {{
    "conceptUnderstanding": [各次考试得分率],
    "formulaApplication": [各次考试得分率],
    "experimentAnalysis": [各次考试得分率],
    "comprehensiveReasoning": [各次考试得分率],
    "mathComputation": [各次考试得分率],
    "informationExtraction": [各次考试得分率]
  }},
  "overallAssessment": "综合评级(A/B/C/D)",
  "nextGoals": ["目标1", "目标2"],
  "recommendedActions": ["具体行动建议1", "具体行动建议2"]
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
