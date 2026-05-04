"""
稍后看 - 后端服务
Flask server that fetches URLs, extracts article content,
categorizes, and estimates reading time.
"""
import re
import os
import json
import math
import time
from datetime import date
import logging
from collections import Counter
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_from_directory

import requests
from bs4 import BeautifulSoup
from readability import Document

import db as database

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='')

# Initialize SQLite database on startup
database.init_db()

# ── Label Presets (parsed from label-prompt.md at startup) ──
_PRESET_LABELS = None  # lazy-loaded cache


def load_preset_labels():
    """Parse label-prompt.md and return list of preset label definitions."""
    global _PRESET_LABELS
    if _PRESET_LABELS is not None:
        return _PRESET_LABELS

    prompt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'label-prompt.md')
    if not os.path.exists(prompt_path):
        logger.warning('label-prompt.md not found, using built-in presets')
        _PRESET_LABELS = _builtin_preset_labels()
        return _PRESET_LABELS

    with open(prompt_path, 'r', encoding='utf-8') as f:
        content = f.read()

    labels = _parse_label_md(content)
    _PRESET_LABELS = labels
    logger.info(f'Loaded {len(labels)} preset labels from label-prompt.md')
    return labels


def _parse_label_md(content):
    """Parse label-prompt.md markdown into structured label definitions."""
    labels = []
    # Parse default priority order
    priority_map = {}
    priority_match = re.search(r'默认优先级为[→\s]*([^。\n]+)', content)
    if priority_match:
        priorities = [p.strip().strip('*') for p in re.split(r'[>＞→]', priority_match.group(1)) if p.strip()]
        for i, name in enumerate(priorities):
            priority_map[name] = len(priorities) - i  # higher = more important

    # Split by "## N. LabelName" sections
    sections = re.split(r'\n##\s+\d+\.\s+', content)
    for section in sections[1:]:  # skip header
        lines = section.strip().split('\n')
        if not lines:
            continue
        name = lines[0].strip()

        # Extract description (blockquote after name)
        desc = ''
        desc_match = re.search(r'>\s*(.+?)(?:\n|$)', section)
        if desc_match:
            desc = desc_match.group(1).strip()

        # Extract keywords
        kw_match = re.search(r'\*\*关键词特征[：:]\*\*\s*(.+?)(?:\n\n|$)', section, re.DOTALL)
        keywords = []
        if kw_match:
            kw_text = kw_match.group(1).strip()
            keywords = [k.strip() for k in re.split(r'[、，,；;]', kw_text) if k.strip()]

        # Build full prompt for LLM classification (the entire section minus the title line)
        prompt_lines = []
        in_section = False
        for line in lines[1:]:
            prompt_lines.append(line)
        prompt = '\n'.join(prompt_lines).strip()

        labels.append({
            'id': name,
            'name': name,
            'description': desc,
            'prompt': prompt,
            'keywords': keywords,
            'priority': priority_map.get(name, 0),
            'is_preset': True,
        })

    # Sort by priority descending
    labels.sort(key=lambda x: -x['priority'])
    return labels


def _builtin_preset_labels():
    """Fallback presets if label-prompt.md is missing."""
    return [
        {'id': '通知', 'name': '通知', 'description': '带有明确时间节点的事项类信息', 'prompt': '通知类：带有明确时间节点的事项，如考试、选课、deadline、报名截止等。', 'keywords': ['截止', '报名', '考试时间', '选课', 'ddl', '提交', '缴费', '开放申请'], 'priority': 6, 'is_preset': True},
        {'id': '专业前沿', 'name': '专业前沿', 'description': '学科技术前沿知识与工具教程', 'prompt': '专业前沿：与学科、技术、职业能力相关的前沿知识，如AI、编程、学术论文、行业研究等。', 'keywords': ['AI', '大模型', '编程', '论文', '教程', '开源', '技术', '学术', '研究'], 'priority': 5, 'is_preset': True},
        {'id': '时政', 'name': '时政', 'description': '公共政策、社会议题、国际关系', 'prompt': '时政：涉及公共政策、政治动态、社会议题、国际关系等。', 'keywords': ['政策', '政府', '教育部', '两会', '外交', '关税', '法案'], 'priority': 4, 'is_preset': True},
        {'id': '文艺', 'name': '文艺', 'description': '以内容深度、审美价值为核心的文章', 'prompt': '文艺：以内容深度、审美价值或情感表达为核心，如书评、影评、随笔、特稿等。', 'keywords': ['书评', '影评', '随笔', '散文', '书单', '深度', '人文', '哲学', '艺术'], 'priority': 3, 'is_preset': True},
        {'id': '攻略', 'name': '攻略', 'description': '可执行的操作指南或消费决策参考', 'prompt': '攻略：提供可执行的操作指南或消费决策参考，如生活技巧、消费测评、旅游攻略、学习方法等。', 'keywords': ['攻略', '推荐', '测评', '方法', '步骤', '怎么', '如何', '省钱', '好物'], 'priority': 2, 'is_preset': True},
        {'id': '娱乐', 'name': '娱乐', 'description': '以消遣放松为目的的轻内容', 'prompt': '娱乐：以消遣放松为目的，话题围绕明星、体育、游戏、网络热点等。', 'keywords': ['明星', '综艺', '比赛', '游戏', '八卦', '网红', 'meme', '星座'], 'priority': 1, 'is_preset': True},
    ]

# ── Headers for fetching ──
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/126.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
}

# ── Label Classification Engine ──

def classify_with_labels_llm(title, text, labels, api_key, api_base, model):
    """Use LLM to classify article into one of the given labels.

    Args:
        labels: list of dicts with 'name' and 'prompt' fields

    Returns dict with 'label', 'confidence', 'all_scores', or None on failure.
    """
    if not api_key or not labels:
        return None

    # Build label definitions for the prompt
    label_defs = []
    for i, lbl in enumerate(labels):
        name = lbl['name']
        prompt = lbl.get('prompt', '') or lbl.get('description', '') or name
        label_defs.append(f'【{name}】{prompt}')

    labels_text = '\n\n'.join(label_defs)
    priority_order = ' > '.join(lbl['name'] for lbl in labels)

    endpoint = api_base.rstrip('/') + '/chat/completions'
    system_prompt = (
        '你是一个专业的文章分类助手。请根据以下标签定义将文章归入最匹配的一个标签。\n\n'
        f'## 标签定义\n{labels_text}\n\n'
        '## 分类规则\n'
        f'1. 仅输出最匹配的一个标签，优先级顺序：{priority_order}\n'
        '2. 置信度范围0-100，低于60输出"未分类"\n'
        '3. 输出格式：标签名|置信度\n'
        '4. 示例输出：通知|85\n'
        '5. 只输出一行「标签名|数字」，不要解释、不要 Markdown、不要序号或多行。\n'
        '6. 置信度口径：90+ 几乎唯一匹配；70–89 较确定；60–69 勉强；低于60必须输出 未分类|置信度。\n'
        '7. 边界提醒：含硬性报名/截止/考试时间窗口→多属「通知」；纯方法论/教程无明确节点→多属「攻略」而非通知。'
    )

    try:
        resp = requests.post(
            endpoint,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': f'标题：{title}\n\n正文（前6000字）：\n{text[:6000]}'},
                ],
                'max_tokens': 100,
                'temperature': 0.1,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if 'choices' not in data or not data['choices']:
            return None

        raw = data['choices'][0]['message']['content'].strip()
        # Parse "标签名|置信度" format
        match = re.match(r'(.+?)\s*\|\s*(\d+)', raw)
        if match:
            label_name = match.group(1).strip()
            confidence = int(match.group(2))
            if label_name == '未分类' or confidence < 60:
                return {'label': '未分类', 'confidence': confidence, 'all_scores': {}}
            # Verify label exists
            valid_names = {l['name'] for l in labels}
            if label_name not in valid_names:
                label_name = '未分类'
            return {'label': label_name, 'confidence': confidence, 'all_scores': {}}
        return None
    except Exception as e:
        logger.warning(f'LLM classification failed: {e}')
        return None


def extract_deadline_with_llm(title, text, api_key, api_base, model):
    """Use LLM to extract deadline date from notification-type articles.

    Returns a date string 'YYYY-MM-DD' or None if no date found.
    """
    if not api_key:
        return None

    endpoint = api_base.rstrip('/') + '/chat/completions'
    system_prompt = (
        '你是一个日期提取助手。从文章内容中找到最关键的截止日期或时间节点。\n'
        '重点关注：报名截止、考试时间、活动日期、申请截止、选课时间、缴费截止、提交节点等。\n'
        '若文中出现多个日期，选取对用户行动最关键的一个，优先级：报名/申请截止 > 考试或活动举行日 > 其他公示日期。\n'
        '输出格式：YYYY-MM-DD（如 2026-05-10）。若没有明确的日期则输出"无"。\n'
        '若仅有「本月底」「下周」等无法唯一定位到日的表述，输出「无」，勿猜测具体日期。\n'
        '只输出日期或"无"，不要输出任何其他内容。'
    )

    today = date.today().isoformat()
    user_deadline = (
        f'今天是 {today}（用于把「本周五」「下周三」「明天」等相对表述解析为 YYYY-MM-DD）。\n\n'
        f'标题：{title}\n\n正文（前4000字）：\n{text[:4000]}'
    )

    try:
        resp = requests.post(
            endpoint,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_deadline},
                ],
                'max_tokens': 30,
                'temperature': 0.1,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if 'choices' not in data or not data['choices']:
            return None

        raw = data['choices'][0]['message']['content'].strip()
        match = re.search(r'(\d{4}-\d{2}-\d{2})', raw)
        if match:
            return match.group(1)
        return None
    except Exception as e:
        logger.warning(f'LLM deadline extraction failed: {e}')
        return None


def classify_with_labels_keywords(title, text, labels):
    """Keyword-based classification using label keyword lists.

    Each label should have a 'keywords' list. Scores by keyword match count,
    normalized by label priority.
    """
    if not labels:
        return {'label': '未分类', 'confidence': 0, 'all_scores': {}}

    combined = (title + ' ' + text[:5000])

    all_scores = {}
    for lbl in labels:
        keywords = lbl.get('keywords', [])
        if not keywords:
            # Auto-extract keywords from label name and description
            desc = lbl.get('description', '') + ' ' + lbl.get('prompt', '')
            keywords = [lbl['name']] + [w for w in re.findall(r'[一-鿿\w]+', desc) if len(w) >= 2]

        matches = 0
        for kw in keywords:
            if kw in combined:
                matches += 1

        # Normalize by keyword count to avoid bias toward labels with many keywords
        norm_score = (matches / max(len(keywords), 1)) * 100
        # Apply priority bonus
        priority_bonus = lbl.get('priority', 0) * 2
        all_scores[lbl['name']] = min(100, round(norm_score + priority_bonus))

    if not all_scores:
        return {'label': '未分类', 'confidence': 0, 'all_scores': {}}

    best_label = max(all_scores, key=all_scores.get)
    best_score = all_scores[best_label]

    if best_score < 25:
        return {'label': '未分类', 'confidence': best_score, 'all_scores': all_scores}

    return {'label': best_label, 'confidence': best_score, 'all_scores': all_scores}


def classify_article(title, text, labels, api_config=None):
    """
    Classify article into the best matching label.

    Args:
        labels: list of label dicts with name, prompt, keywords, priority
        api_config: optional dict with api_key, api_base, model for LLM classification

    Returns dict with: label, confidence, all_scores
    """
    if not labels:
        return {'label': '未分类', 'confidence': 0, 'all_scores': {}}

    # Try LLM first if configured
    api_cfg = api_config or {}
    if api_cfg.get('api_key'):
        result = classify_with_labels_llm(
            title, text, labels,
            api_key=api_cfg['api_key'],
            api_base=api_cfg.get('api_base', 'https://api.deepseek.com/v1'),
            model=api_cfg.get('model', 'deepseek-chat'),
        )
        if result:
            return result

    # Fall back to keyword matching
    return classify_with_labels_keywords(title, text, labels)


def estimate_read_time(text, content_type='article', label='', video_duration_min=None):
    """Estimate reading time in minutes.

    Video: use actual duration if available, else estimate from companion text.
    Article: base 400 chars/min for Chinese. 专业前沿 250 chars/min, 文艺 350 chars/min.
    English: ~200 words/min always.
    """
    # Video with known duration
    if content_type == 'video' and video_duration_min is not None:
        return max(1, int(round(video_duration_min)))

    chinese_chars = len(re.findall(r'[一-鿿]', text))
    english_words = len(re.findall(r'[a-zA-Z]+', text))

    # Adjust speed by label
    if label == '专业前沿':
        cpm = 250  # technical content reads slower
    elif label == '文艺':
        cpm = 350  # literary content reads slightly slower
    else:
        cpm = 400

    minutes = (chinese_chars / cpm) + (english_words / 200)
    return max(1, round(minutes))


def extract_video_duration(soup, html=''):
    """Try to extract video duration in minutes from page metadata.

    Checks: og:video:duration, schema.org VideoObject JSON-LD,
    itemprops, and common site-specific patterns.
    Returns float minutes or None.
    """
    if not soup:
        return None

    # 1. OpenGraph og:video:duration (seconds as integer)
    for selector in [
        'meta[property="og:video:duration"]',
        'meta[property="video:duration"]',
    ]:
        el = soup.select_one(selector)
        if el:
            val = (el.get('content', '') or el.get('value', '')).strip()
            if val and val.isdigit():
                return int(val) / 60

    # 2. Schema.org itemprops
    el = soup.select_one('[itemprop="duration"]')
    if el:
        dur = (el.get('content', '') or el.get('datetime', '') or el.get_text()).strip()
        minutes = _parse_iso8601_duration(dur)
        if minutes:
            return minutes

    # 3. JSON-LD VideoObject
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
            if isinstance(data, dict):
                dur = data.get('duration', '')
            elif isinstance(data, list):
                dur = data[0].get('duration', '') if data else ''
            else:
                dur = ''
            if dur:
                minutes = _parse_iso8601_duration(str(dur))
                if minutes:
                    return minutes
        except (json.JSONDecodeError, TypeError):
            pass

    # 4. Site-specific: B站 __INITIAL_STATE__
    if html:
        import re as re_module
        m = re_module.search(r'"duration"\s*:\s*(\d+)', html)
        if m:
            return int(m.group(1)) / 60

    return None


def _parse_iso8601_duration(dur):
    """Parse ISO 8601 duration string like 'PT1H23M45S' or 'PT5M30S' into minutes."""
    if not dur:
        return None
    import re as _re
    m = _re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', dur.strip())
    if not m:
        return None
    h, mi, s = (int(m.group(i) or 0) for i in (1, 2, 3))
    if h == 0 and mi == 0 and s == 0:
        return None
    return h * 60 + mi + s / 60


def _format_duration(minutes):
    """Format minutes into a human-readable Chinese string."""
    m = int(round(minutes))
    if m < 1:
        return '不到1分钟'
    if m < 60:
        return f'{m}分钟'
    h = m // 60
    remain = m % 60
    if remain == 0:
        return f'{h}小时'
    return f'{h}小时{remain}分钟'


def _tokenize_chinese(s):
    """Tokenize Chinese text into character unigrams + bigrams for TF-IDF."""
    chars = re.findall(r'[一-鿿]', s)
    tokens = list(chars)
    tokens += [chars[i] + chars[i+1] for i in range(len(chars)-1)]
    return tokens


def _jaccard_similarity(tokens1, tokens2):
    """Compute Jaccard similarity between two token sets for MMR diversity."""
    s1, s2 = set(tokens1), set(tokens2)
    if not s1 or not s2:
        return 0.0
    return len(s1 & s2) / len(s1 | s2)


def _is_reserved_domain(url):
    """Block RFC 2606 reserved domains and common placeholder domains."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ''
    hostname_lower = hostname.lower()
    reserved = {
        'example.com', 'example.org', 'example.net',
        'test.com', 'localhost', 'placeholder.com',
        'sample.com', 'demo.com', 'mysite.com',
    }
    if hostname_lower in reserved:
        return True
    # Also catch subdomains like www.example.com, sub.example.org
    parts = hostname_lower.split('.')
    for i in range(len(parts) - 1):
        candidate = '.'.join(parts[i:])
        if candidate in reserved:
            return True
    return False


def _is_garbled_content(text):
    """Detect content that is mostly WeChat UI boilerplate rather than actual article text."""
    if not text or len(text) < 20:
        return True

    # Count lines that are clearly WeChat/platform UI boilerplate
    wechat_markers = [
        '轻点两下取消赞', '轻点两下取消在看',
        '微信扫一扫', '使用小程序',
        '关注该公众号', '关注公众号',
        '写下你的留言', '写留言',
        '精选留言', '朋友留言',
        '以上内容为广告', '广告推广',
        '扫描二维码', '长按识别二维码',
        '点击上方', '点击下方',
        '关注我们', '欢迎关注',
        '原文链接', '阅读原文',
        '查看原文', '查看更多',
        '收录于合集',
        '继续滑动看下一个',
    ]
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return True

    boilerplate_count = 0
    for line in lines:
        for marker in wechat_markers:
            if marker in line:
                boilerplate_count += 1
                break

    # Only reject if boilerplate lines dominate (>= 60% of all lines)
    if len(lines) > 0 and boilerplate_count >= len(lines) * 0.6:
        return True

    # Also check: if the content is very short and all boilerplate
    if len(text) < 100 and boilerplate_count >= len(lines) * 0.4:
        return True

    # Ratio of punctuation-only lines (catches pure-symbol noise)
    if len(lines) > 5:
        punct_only = sum(1 for l in lines if re.match(r'^[，。！？、：；""''「」『』【】《》（）\s,.!?:;]+$', l))
        if punct_only > len(lines) * 0.4:
            return True

    return False


def extract_summary(text, max_sentences=3):
    """
    Improved extractive summarization using TF-IDF scoring, position weighting,
    cue phrase detection, boilerplate filtering, and MMR for diversity.
    """
    if not text or len(text) < 100:
        return text[:300] if text else ''

    # ── Pre-clean: remove known boilerplate lines ──
    text = re.sub(r'[（(]?(图|图源|图片|素材|来源|编辑|排版|责编|审核|撰文)[：:][^。）\n]{2,50}[）)]?', '', text)
    text = re.sub(r'[（(]?(推广|合作|商务|广告)[：:][^。）\n]{2,80}[）)]?', '', text)
    text = re.sub(r'(微信号|微信ID|WeChat)[：:]\s*\w+', '', text)
    text = re.sub(r'创作不易[，,。.!！]*?(点个赞|点在看|求点赞|求转发)?[^。]{0,20}', '', text)

    # Split into sentences
    raw_sentences = re.split(r'[。！？!?\n](?![）\)])', text)
    sentences = []
    for s in raw_sentences:
        s = s.strip()
        # Skip sentences that look like UI boilerplate, quotes, or examples
        if len(s) < 12 or len(s) > 600:
            continue
        if not any('一' <= c <= '鿿' or c.isalpha() for c in s):
            continue
        # Filter quoted speech / example dialogues
        if s.startswith('"') or s.startswith('「') or s.startswith('“'):
            continue
        if re.match(r'^[>＞]', s):
            continue
        # Filter pure example markers
        if re.match(r'^(比如|例如|举个|比如说)', s):
            continue
        sentences.append(s)

    if not sentences:
        return ''

    if len(sentences) <= max_sentences:
        return '。'.join(sentences) + '。'

    # ── Tokenize all sentences ──
    sent_tokens = [_tokenize_chinese(s) for s in sentences]

    # ── Compute IDF ──
    n_docs = len(sentences)
    doc_count = Counter()
    for tokens in sent_tokens:
        for t in set(tokens):
            doc_count[t] += 1

    def idf(token):
        return math.log((n_docs + 1.0) / (doc_count.get(token, 0) + 1.0)) + 1.0

    # ── Score each sentence ──
    BOILERPLATE = {
        # WeChat / social media UI
        '点击', '关注', '扫码', '阅读原文', '转发', '点赞', '在看',
        '评论', '分享', '小程序', '视频', '广告', '推荐', '戳',
        '二维码', '回复', '后台', '星标', '置顶', '订阅', '入群',
        '领取', '免费', '福利', '优惠', '拼团',
        # Promotion / business text
        '推广', '合作', '商务', '微信号', '微信：', '加微信',
        'directubeee', '创作不易', '版权所有', '侵权', '转载',
        '请联系', '商务合作', '广告投放', '招商', '赞助',
        '免责声明', '版权归', '归原作者', '素材来源', '图片来自',
        '图源', '来源：', '来源:', '编辑：', '排版：',
        '如需转载', '未经授权', '不得转载', '严禁转载',
        # Interactive prompts
        '点个赞', '点在看', '收藏起来', '分享给', '转发到',
        '评论区', '留言区', '欢迎留言', '互动', '打卡',
        # Low-value meta text
        '往期回顾', '精选推荐', '你可能还想看', '猜你喜欢',
        '热门文章', '查看历史', '设为星标', '常读订阅号',
    }
    CUE_PHRASES = {
        '关键', '重要', '核心', '本质', '总结', '因此', '所以',
        '建议', '方法', '原理', '意味着', '换句话说', '也就是',
        '值得注意', '需要注意的是', '必须', '应该', '可以', '能够',
        '事实上', '实际上', '最终', '结论', '总之', '概括',
    }

    scored = []
    for i, (sent, tokens) in enumerate(zip(sentences, sent_tokens)):
        score = 0.0

        # 1. TF-IDF score (importance of content)
        if tokens:
            tf = Counter(tokens)
            tfidf_total = sum(tf[t] * idf(t) for t in set(tokens))
            score += (tfidf_total / len(tokens)) * 12.0

        # 2. Position score (strong lead bias + conclusion bonus)
        ratio = i / max(len(sentences) - 1, 1)
        if ratio < 0.10:
            score += 4.0       # First 10% — usually contains the thesis
        elif ratio < 0.20:
            score += 2.0       # Second 10% — supporting context
        elif ratio < 0.35:
            score += 0.8       # Middle transitions
        if ratio > 0.85:
            score += 2.5       # Closing sentences — often conclusion/summary

        # 3. Cue phrase bonus
        for cue in CUE_PHRASES:
            if cue in sent:
                score += 0.6

        # 4. Boilerplate penalty
        for bp in BOILERPLATE:
            if bp in sent:
                score -= 3.0

        # 5. Length optimization (prefer 25-250 chars)
        length = len(sent)
        if 30 <= length <= 250:
            score += 0.5
        elif length < 20 or length > 500:
            score -= 1.5

        # 6. Sentences with numbers/dates often carry key information
        if re.search(r'\d+', sent):
            score += 0.3

        scored.append((i, score, sent, tokens))

    # ── MMR selection: maximize relevance, minimize redundancy ──
    selected = []
    remaining = list(scored)

    LAMBDA = 0.7  # relevance vs diversity tradeoff

    while len(selected) < max_sentences and remaining:
        best_item = None
        best_mmr = -float('inf')

        for item in remaining:
            _, base_score, _, tokens = item
            # Compute redundancy penalty against already-selected sentences
            redundancy = 0.0
            for sel_item in selected:
                sim = _jaccard_similarity(tokens, sel_item[3])
                redundancy = max(redundancy, sim)

            mmr = LAMBDA * base_score - (1 - LAMBDA) * redundancy * 6.0

            if mmr > best_mmr:
                best_mmr = mmr
                best_item = item

        if best_item:
            selected.append(best_item)
            remaining.remove(best_item)

    # Sort by original position for natural reading order
    selected.sort(key=lambda x: x[0])

    # ── Assemble output ──
    result = '。'.join(s[2] for s in selected) + '。'

    # Clean up: remove common artifacts
    result = re.sub(r'\.{3,}', '', result)
    result = re.sub(r'\s+', '', result)

    return result


def is_wechat_url(url):
    """Check if URL is from WeChat public account."""
    parsed = urlparse(url)
    netloc = parsed.netloc
    return netloc in ('mp.weixin.qq.com', 'mp.weixinbridge.com') or netloc.endswith('.mp.weixin.qq.com')


def extract_wechat_article(soup, html):
    """Extract content specifically from WeChat public account articles."""
    # Title
    title = ''
    for selector in ['#activity-name', 'meta[property="og:title"]', 'title']:
        el = soup.select_one(selector)
        if el:
            title = el.get('content', '') or el.get_text()
            if title:
                break

    # Author
    author = ''
    for selector in ['#js_name', 'meta[property="og:article:author"]', '#js_author_name']:
        el = soup.select_one(selector)
        if el:
            author = el.get('content', '') or el.get_text()
            if author:
                break

    # Content
    content = ''
    content_el = soup.select_one('#js_content')
    if content_el:
        # Remove hidden elements
        for hidden in content_el.select('[style*="visibility:hidden"], [style*="display:none"]'):
            hidden.decompose()
        content = content_el.get_text(separator='\n', strip=True)
        # If WeChat content is garbled (e.g. blocked by login wall), try general extractor
        if _is_garbled_content(content):
            doc = Document(html)
            content = BeautifulSoup(doc.summary(), 'lxml').get_text(separator='\n', strip=True)
    else:
        # Fallback to readability
        doc = Document(html)
        content = BeautifulSoup(doc.summary(), 'lxml').get_text(separator='\n', strip=True)

    # Publish time
    pub_time = ''
    time_el = soup.select_one('#publish_time')
    if time_el:
        pub_time = time_el.get_text(strip=True)

    # If still garbled after fallback, use empty content so caller can reject
    if _is_garbled_content(content):
        content = ''

    return {
        'title': title.strip() if title else '未命名文章',
        'author': author.strip() if author else '微信公众号',
        'content': content.strip() if content else '',
        'pub_time': pub_time,
        'source': '微信公众号',
    }


def extract_general_article(soup, html, url):
    """Extract content from general web articles using readability."""
    # Try Open Graph first
    title = ''
    og_title = soup.select_one('meta[property="og:title"]')
    if og_title:
        title = og_title.get('content', '')

    if not title:
        title_tag = soup.select_one('title')
        if title_tag:
            title = title_tag.get_text(strip=True)

    # Author
    author = ''
    for sel in ['meta[property="og:article:author"]', 'meta[name="author"]',
                'meta[property="author"]']:
        el = soup.select_one(sel)
        if el:
            author = el.get('content', '')
            if author:
                break

    # Content via readability
    try:
        doc = Document(html)
        content_html = doc.summary()
        content_soup = BeautifulSoup(content_html, 'lxml')
        content = content_soup.get_text(separator='\n', strip=True)
        if not title:
            title = doc.title()
    except Exception:
        content = soup.body.get_text(separator='\n', strip=True)[:10000] if soup.body else ''

    # Source
    parsed = urlparse(url)
    source_map = {
        'zhuanlan.zhihu.com': '知乎专栏',
        'www.jianshu.com': '简书',
        'www.douban.com': '豆瓣',
        'www.bilibili.com': 'B站',
        'www.qq.com': '腾讯新闻',
        'new.qq.com': '腾讯新闻',
        '36kr.com': '36氪',
        'sspai.com': '少数派',
        'www.geeksforgeeks.org': 'GeeksforGeeks',
        'medium.com': 'Medium',
    }
    source = source_map.get(parsed.netloc, parsed.netloc.replace('www.', ''))

    return {
        'title': title.strip() if title else '未命名文章',
        'author': author.strip() if author else source,
        'content': content,
        'pub_time': '',
        'source': source,
    }


def summarize_with_llm(title, text, api_key, api_base, model):
    """Use an LLM (OpenAI-compatible API) to generate an abstractive summary.

    Args:
        title: article title
        text: article content (truncated)
        api_key: user-provided API key
        api_base: API base URL
        model: model name

    Returns summary string, or None on failure.
    """
    if not api_key:
        return None

    # Skip if text looks like garbled noise (saves API cost + prevents junk output)
    if _is_garbled_content(text):
        logger.warning('Skipping LLM summary: text appears garbled')
        return None

    endpoint = api_base.rstrip('/') + '/chat/completions'

    prompt = (
        '请用2-3句话概括下面这篇文章的核心内容。要求：\n'
        '1. 第一句点明文章主题和核心观点\n'
        '2. 后续句子补充关键论据或结论\n'
        '3. 语言简洁有力，不要出现"这篇文章"、"本文"、"作者"等元描述\n'
        '4. 只输出摘要本身，不要加任何前缀或后缀\n'
        '5. 全文不超过 150 字；不得编造正文中没有的核心事实或具体数据；正文过少时可结合标题概括，避免臆测细节\n'
        f'\n文章标题：{title}\n\n'
        f'文章内容（前8000字）：\n{text[:8000]}'
    )

    try:
        resp = requests.post(
            endpoint,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': model,
                'messages': [
                    {
                        'role': 'system',
                        'content': (
                            '你是一个专业的中文内容编辑，擅长用最精炼的语言提炼文章核心观点。'
                            '严格依据正文与标题，不捏造未出现的信息。'
                        ),
                    },
                    {'role': 'user', 'content': prompt},
                ],
                'max_tokens': 350,
                'temperature': 0.3,
            },
            timeout=25,
        )
        if resp.status_code != 200:
            return ''
        data = resp.json()
        if 'choices' in data and len(data['choices']) > 0:
            summary = data['choices'][0]['message']['content'].strip()
            # Strip any "摘要：" or similar prefixes the model might add
            summary = re.sub(r'^(摘要|总结|概括|核心内容)[：:]\s*', '', summary)
            logger.info(f'LLM summary generated ({len(summary)} chars)')
            return summary
        else:
            logger.warning(f'LLM API error: {data}')
            return None
    except Exception as e:
        logger.warning(f'LLM summarization failed: {e}')
        return None


# ── Content Type Detection ──

_CONTENT_TYPE_PATTERNS = [
    ('video', [
        # Video platforms
        'bilibili.com', 'youtube.com', 'youtu.be', 'v.qq.com', 'iqiyi.com',
        'youku.com', 'douyin.com', 'kuaishou.com', 'acfun.cn', 'tv.sohu.com',
        'mg.tv', 'mgtv.com', 'vimeo.com',
    ]),
    ('image', [
        # Image/design platforms
        'zcool.com.cn', 'huaban.com', 'pinterest.com', 'dribbble.com',
        'behance.net', '500px.com', 'tuchong.com', 'duitang.com',
        'poocg.com', 'pixiv.net',
    ]),
    ('article', [
        # Article / reading platforms
        'mp.weixin.qq.com', 'zhihu.com', 'jianshu.com', 'csdn.net',
        'juejin.cn', 'segmentfault.com', 'sspai.com', '36kr.com',
        'geekpark.net', 'ifanr.com', 'huxiu.com', 'infoq.cn',
        'medium.com', 'nytimes.com', 'thepaper.cn', 'sohu.com/a/',
    ]),
]


def detect_content_type(url, html='', soup=None):
    """
    Detect the content type of a URL: video | image | article | other.
    Priority: og:type meta > URL domain patterns > page content analysis.
    """
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()

    # 1. Check og:type meta tag (most reliable when present)
    if soup:
        og_type = None
        for selector in [
            'meta[property="og:type"]',
            'meta[name="og:type"]',
            'meta[property="og:video"]',
        ]:
            el = soup.select_one(selector)
            if el:
                og_type = (el.get('content', '') or el.get('value', '')).lower()
                if og_type:
                    break
        if og_type:
            if og_type.startswith('video') or og_type == 'video.other':
                return 'video'
            if og_type.startswith('image') or og_type == 'photo':
                return 'image'
            if og_type.startswith('article') or og_type in ('blog', 'news'):
                return 'article'

    # 2. Check URL domain patterns
    for content_type, patterns in _CONTENT_TYPE_PATTERNS:
        for pattern in patterns:
            if pattern in domain:
                return content_type

    # 3. Check page content cues (when HTML is available)
    if html and soup:
        # Video: presence of <video> tag or video player wrappers
        if soup.select('video, .video-player, .player-container, [data-video-id]'):
            return 'video'
        if soup.select('meta[itemprop="video"], meta[property="og:video:url"]'):
            return 'video'

        # Image: image-heavy page with little text
        if soup:
            imgs = len(soup.select('img.gallery, .photo-item img, .image-item img'))
            if imgs > 10:
                return 'image'

    # 4. URL extension hints
    if path.endswith(('.mp4', '.webm', '.mov', '.avi', '.mkv')):
        return 'video'
    if path.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp')):
        return 'image'

    # 5. Common article paths
    article_paths = ['/article/', '/p/', '/post/', '/posts/', '/news/', '/a/', '/blog/']
    for ap in article_paths:
        if path.startswith(ap) or ap in path:
            return 'article'

    # Default
    return 'article'


def fetch_and_extract(url, api_config=None, labels=None):
    """Main function: fetch URL and extract article data."""
    logger.info(f'Fetching: {url}')

    # Defense-in-depth: block reserved domains before any network request
    if _is_reserved_domain(url):
        return {'success': False, 'error': '该域名是保留示例域名，无法解析为真实文章。'}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        raw_bytes = resp.content

        # Detect encoding: 1) HTML meta charset  2) UTF-8 for Chinese sites  3) apparent
        encoding = None
        # Scan first 2KB for <meta charset> or <meta http-equiv="Content-Type">
        head = raw_bytes[:2048]
        meta_match = re.search(
            rb'<meta[^>]+charset=["\']?([a-zA-Z0-9\-_]+)["\']?',
            head, re.IGNORECASE
        )
        if meta_match:
            encoding = meta_match.group(1).decode('ascii', errors='ignore')
        elif is_wechat_url(url):
            encoding = 'utf-8'  # WeChat always uses UTF-8
        else:
            # Try UTF-8 first for .cn / Chinese-looking domains
            parsed = urlparse(url)
            if parsed.netloc.endswith('.cn') or any(
                kw in parsed.netloc for kw in ['zhihu', 'weixin', 'qq', 'baidu', 'csdn', 'jianshu']
            ):
                encoding = 'utf-8'

        if not encoding:
            encoding = resp.apparent_encoding or 'utf-8'

        html = raw_bytes.decode(encoding, errors='replace')
    except requests.exceptions.Timeout:
        return {'success': False, 'error': '请求超时，请检查链接是否可访问，或尝试直接粘贴文章内容。'}
    except requests.exceptions.ConnectionError:
        return {'success': False, 'error': '无法连接到该网站，请确认链接是否正确。'}
    except requests.exceptions.TooManyRedirects:
        return {'success': False, 'error': '重定向过多，请直接粘贴文章内容。'}
    except Exception as e:
        logger.error(f'Fetch error: {e}')
        return {'success': False, 'error': f'获取文章失败，请尝试直接粘贴内容。'}

    if len(html) < 500:
        return {'success': False, 'error': '获取到的页面内容为空或过短，可能是动态加载页面。'}

    soup = BeautifulSoup(html, 'lxml')

    # Determine extractor
    if is_wechat_url(url):
        article = extract_wechat_article(soup, html)
    else:
        article = extract_general_article(soup, html, url)

    content = article['content']

    if not content or len(content) < 50:
        # Try body as fallback
        if soup.body:
            content = soup.body.get_text(separator='\n', strip=True)[:5000]
        article['content'] = content

    if not content or len(content) < 20:
        return {'success': False, 'error': '未能提取到文章正文，请尝试直接粘贴内容。'}

    if _is_garbled_content(content):
        return {'success': False, 'error': '提取到的内容包含乱码，页面可能需要登录或扫码查看，请尝试直接粘贴内容。'}

    # Limit text length for analysis (avoid OOM on very long pages)
    analysis_text = content[:15000]

    # Content preview (first 300 chars)
    content_preview = content[:300].strip()

    # ── Content Type Detection (before label/read-time so they can use it) ──
    content_type = detect_content_type(url, html, soup)

    # ── Video Duration (if applicable) ──
    video_duration_min = None
    if content_type == 'video':
        video_duration_min = extract_video_duration(soup, html)

    # ── Label Classification ──
    all_labels = labels or load_preset_labels()
    classification = classify_article(article['title'], analysis_text, all_labels, api_config)

    # Estimate reading time (uses content_type + label for speed adjustment)
    read_time = estimate_read_time(content, content_type, classification['label'], video_duration_min)

    # Generate summary — try LLM first (if user provided API config), fall back to extractive
    api_cfg = api_config or {}
    llm_summary = summarize_with_llm(
        article['title'], analysis_text[:8000],
        api_key=api_cfg.get('api_key', ''),
        api_base=api_cfg.get('api_base', 'https://api.deepseek.com/v1'),
        model=api_cfg.get('model', 'deepseek-chat'),
    )
    if llm_summary:
        summary = llm_summary
        summary_mode = 'llm'
    else:
        summary = extract_summary(analysis_text)
        summary_mode = 'extractive'
    # Ensure summary is never empty
    if not summary or not summary.strip():
        summary = content_preview[:200].strip()
        summary_mode = 'extractive'

    return {
        'success': True,
        'article': {
            'title': article['title'],
            'author': article['author'],
            'source': article['source'],
            'label': classification['label'],
            'label_confidence': classification['confidence'],
            'label_scores': classification['all_scores'],
            'read_time_min': read_time,
            'read_time_display': _format_duration(read_time),
            'summary': summary,
            'summary_mode': summary_mode,
            'content_preview': content_preview,
            'content': content,
            'url': url,
            'content_type': content_type,
        }
    }


# ── Routes ──

@app.route('/')
def index():
    """Serve the main HTML page."""
    response = send_from_directory('static', 'index.html')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/api/analyze', methods=['POST'])
def analyze():
    """Analyze a URL and return article data. Persists to database."""
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'success': False, 'error': '请提供文章链接。'}), 400

    url = data['url'].strip()
    if not url:
        return jsonify({'success': False, 'error': '请提供文章链接。'}), 400

    # Parse and validate URL scheme
    parsed = urlparse(url)
    if parsed.scheme:
        if parsed.scheme not in ('http', 'https'):
            return jsonify({'success': False, 'error': '不支持的链接协议，请输入 http/https 链接。'}), 400
    elif not url.startswith(('http://', 'https://')):
        # No scheme present — only auto-add https if it looks like a domain
        if '://' in url:
            return jsonify({'success': False, 'error': '不支持的链接协议，请输入 http/https 链接。'}), 400
        url = 'https://' + url

    # Block RFC 2606 reserved domains (example.com, etc.)
    if _is_reserved_domain(url):
        return jsonify({'success': False, 'error': '该域名是保留示例域名，无法解析为真实文章。'}), 400

    # Check for duplicate in database
    existing = database.check_duplicate_url(url)
    if existing:
        return jsonify({
            'success': False,
            'error': '这篇文章已经存在于你的库中',
            'duplicate': True,
            'article': existing,
        }), 409

    api_config = data.get('api_config', {})
    user_labels = data.get('labels', None)
    result = fetch_and_extract(url, api_config, user_labels)

    if not result.get('success'):
        return jsonify(result), 400

    article = result['article']

    # Save to database (catch race condition on duplicate URL)
    try:
        article_id = database.insert_article(article)
    except Exception:
        existing = database.check_duplicate_url(url)
        if existing:
            return jsonify({
                'success': False,
                'error': '这篇文章已经存在于你的库中',
                'duplicate': True,
                'article': existing,
            }), 409
        raise

    # Set initial user_labels if AI assigned a label
    if article.get('label') and article['label'] != '未分类':
        database.set_article_labels(article_id, [article['label']])

    # If classified as 通知, try LLM deadline extraction and flag for confirmation
    saved = database.get_article(article_id)
    if article.get('label') == '通知':
        api_key = api_config.get('api_key', '')
        api_base = api_config.get('api_base', '')
        model = api_config.get('model', '')
        extracted = None
        if api_key:
            extracted = extract_deadline_with_llm(
                article.get('title', ''),
                article.get('content', article.get('content_preview', '')),
                api_key, api_base, model
            )
        # Always flag for confirmation; attach extracted date if found, else null
        saved['ddl_pending'] = True
        if extracted:
            saved['extracted_deadline'] = extracted
        # Persist to DB so DDL confirmation survives page reload
        db_updates = {'ddl_pending': True}
        if extracted:
            db_updates['extracted_deadline'] = extracted
        database.update_article(article_id, db_updates)
    return jsonify({'success': True, 'article': saved}), 201


@app.route('/api/articles', methods=['GET'])
def list_articles():
    """List articles with optional filter, search, and pagination."""
    filter = request.args.get('filter', 'all')
    search = request.args.get('search', '')
    try:
        limit = max(1, min(int(request.args.get('limit', 50)), 200))
        offset = max(int(request.args.get('offset', 0)), 0)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'limit 和 offset 必须为整数'}), 400
    articles, total = database.get_articles(filter=filter, search=search, limit=limit, offset=offset)
    return jsonify({
        'success': True,
        'articles': articles,
        'total': total,
        'filter': filter,
        'limit': limit,
        'offset': offset,
    })


@app.route('/api/articles/<int:article_id>', methods=['GET'])
def get_article(article_id):
    """Get a single article by ID with full content."""
    article = database.get_article(article_id)
    if not article:
        return jsonify({'success': False, 'error': '文章不存在'}), 404
    return jsonify({'success': True, 'article': article})


def _get_all_known_label_names():
    """Return set of all known label names (presets + custom) for validation."""
    known = set()
    try:
        presets = load_preset_labels()
        for p in presets:
            known.add(p['name'])
    except Exception:
        pass
    # Also include custom labels from config
    label_cfg = database.get_config('label_config') or {}
    for lbl in label_cfg.get('customLabels', []):
        if isinstance(lbl, dict) and 'name' in lbl:
            known.add(lbl['name'])
    for lbl in label_cfg.get('subLabels', []):
        if isinstance(lbl, dict) and 'name' in lbl:
            known.add(lbl['name'])
    known.add('未分类')
    return known


@app.route('/api/articles/<int:article_id>', methods=['PUT'])
def update_article(article_id):
    """Update article metadata (read status, labels, etc.)."""
    updates = request.get_json()
    if not updates:
        return jsonify({'success': False, 'error': '请提供要更新的字段'}), 400

    # Validate label against known labels to prevent stored XSS
    if 'label' in updates:
        label_value = updates['label']
        valid_labels = _get_all_known_label_names()
        if label_value and label_value != '未分类' and label_value not in valid_labels:
            return jsonify({'success': False, 'error': f'无效的标签: {label_value}'}), 400

    if 'userLabels' in updates:
        user_labels = updates['userLabels']
        if isinstance(user_labels, list):
            valid_labels = _get_all_known_label_names()
            for lbl in user_labels:
                if lbl and lbl != '未分类' and lbl not in valid_labels:
                    return jsonify({'success': False, 'error': f'无效的用户标签: {lbl}'}), 400

    article = database.update_article(article_id, updates)
    if not article:
        return jsonify({'success': False, 'error': '文章不存在'}), 404
    return jsonify({'success': True, 'article': article})


@app.route('/api/articles/<int:article_id>', methods=['DELETE'])
def delete_article(article_id):
    """Delete an article."""
    deleted = database.delete_article(article_id)
    if not deleted:
        return jsonify({'success': False, 'error': '文章不存在'}), 404
    return jsonify({'success': True, 'deleted_id': article_id})


@app.route('/api/articles/batch-delete', methods=['POST'])
def batch_delete_articles():
    """Delete multiple articles at once."""
    data = request.get_json()
    if not data or 'ids' not in data:
        return jsonify({'success': False, 'error': '请提供文章ID列表'}), 400
    ids = data['ids']
    if not isinstance(ids, list) or len(ids) == 0:
        return jsonify({'success': False, 'error': 'ID列表不能为空'}), 400
    count = database.delete_articles_batch(ids)
    return jsonify({'success': True, 'deleted': count})


@app.route('/api/config', methods=['GET'])
def get_config():
    """Get all user configuration."""
    llm_cfg = database.get_config('llm_config') or {}
    label_cfg = database.get_config('label_config') or {}
    reminder_cfg = database.get_config('reminder_config') or {}
    # Mask API key
    if llm_cfg.get('api_key'):
        key = llm_cfg['api_key']
        llm_cfg['api_key'] = key[:4] + '***' + key[-4:] if len(key) > 8 else '***'
    return jsonify({
        'success': True,
        'config': {
            'llm_config': llm_cfg,
            'label_config': label_cfg,
            'reminder_config': reminder_cfg,
        }
    })


@app.route('/api/config', methods=['PUT'])
def update_config():
    """Save user configuration."""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请提供配置数据'}), 400

    if 'llm_config' in data:
        database.set_config('llm_config', data['llm_config'])
    if 'label_config' in data:
        lc = data['label_config']
        # Validate label names in labelOrder / topLabels / hiddenLabels
        known = set()
        try:
            for p in load_preset_labels():
                known.add(p['name'])
        except Exception:
            pass
        known.add('未分类')
        for lbl in lc.get('customLabels', []):
            if isinstance(lbl, dict) and 'name' in lbl:
                n = lbl['name']
                if not isinstance(n, str) or '<' in n or '>' in n or len(n) > 100 or not n.strip():
                    return jsonify({'success': False, 'error': f'无效的自定义标签名: {n}'}), 400
                known.add(n)
        for lbl in lc.get('subLabels', []):
            if isinstance(lbl, dict) and 'name' in lbl:
                n = lbl['name']
                if not isinstance(n, str) or '<' in n or '>' in n or len(n) > 100 or not n.strip():
                    return jsonify({'success': False, 'error': f'无效的子标签名: {n}'}), 400
                known.add(n)
        for key in ['labelOrder', 'topLabels', 'hiddenLabels']:
            vals = lc.get(key, [])
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, str) and v not in known:
                        return jsonify({'success': False, 'error': f'未知标签: {v}'}), 400
        database.set_config('label_config', lc)
    if 'reminder_config' in data:
        database.set_config('reminder_config', data['reminder_config'])

    return jsonify({'success': True})


@app.route('/api/stats', methods=['GET'])
def stats():
    """Get article statistics."""
    return jsonify({'success': True, 'stats': database.get_stats()})


@app.route('/api/recommend/top', methods=['GET'])
def recommend_top():
    """Return the single highest-scored article for push notifications."""
    article = database.get_top_article()
    if not article:
        return jsonify({'success': False, 'error': 'No recommendable articles'}), 404
    article.pop('full_content', None)
    return jsonify({'success': True, 'article': article})


@app.route('/api/recommend/startup', methods=['GET'])
def recommend_startup():
    """Return personalized top article for startup greeting (no time factors).
    Accepts ?exclude=id1,id2 to skip already-recommended articles in this session."""
    exclude_str = request.args.get('exclude', '')
    exclude_ids = []
    if exclude_str:
        try:
            exclude_ids = [int(x.strip()) for x in exclude_str.split(',') if x.strip()]
        except ValueError:
            pass
    articles = database.get_personalized_top(limit=1 + len(exclude_ids) + 5, exclude_ids=exclude_ids)
    if not articles:
        return jsonify({'success': False, 'error': 'No recommendable articles'}), 404
    article = articles[0]
    article.pop('full_content', None)
    return jsonify({'success': True, 'article': article})


# ── LLM-over-DB Query ──

def _is_safe_sql(sql):
    """Ensure SQL is a read-only SELECT statement."""
    sql_upper = sql.strip().upper()
    if not sql_upper.startswith('SELECT'):
        return False
    # Strip SQL block comments that could split keywords: /**/DROP/**/TABLE
    sql_clean = re.sub(r'/\*.*?\*/', '', sql_upper)
    forbidden = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE',
                 'ATTACH', 'DETACH', 'PRAGMA', 'REPLACE', 'TRUNCATE',
                 'EXEC', 'LOAD', 'IMPORT', 'VACUUM', 'GRANT', 'REVOKE']
    for kw in forbidden:
        if re.search(r'(?:^|[\s(;])' + re.escape(kw) + r'(?:$|[\s(;])', sql_clean):
            logger.warning(f'Blocked SQL with forbidden keyword: {kw}')
            return False
    return True


def _sanitize_sql(raw):
    """Strip markdown code fences and LLM artifacts from generated SQL."""
    raw = raw.strip()
    # Remove ```sql ... ``` or ``` ... ``` wrappers
    if raw.startswith('```'):
        raw = re.sub(r'^```\w*\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    return raw.strip()


def _format_articles_for_llm(articles, max_per=10):
    """Format article list for LLM consumption, including unified score and rich status."""
    lines = []
    for i, a in enumerate(articles[:max_per], 1):
        status = '已读' if a.get('is_read') else '未读'
        score = a.get('_score', '?')
        dismissed = a.get('dismiss_count', 0)
        snoozed = a.get('snooze_until', '')
        flags = []
        if dismissed: flags.append(f'忽略{dismissed}次')
        if snoozed: flags.append(f'延后至{snoozed[:10]}')
        # Check for opened-but-unfinished
        if a.get('_opened_unfinished'):
            flags.append('上次未看完')
        flag_str = (' [' + ', '.join(flags) + ']') if flags else ''
        lines.append(
            f"{i}. [ID:{a.get('id','')}] score={score} 《{a.get('title', '')}》 | "
            f"标签:{a.get('label','')} | "
            f"类型:{a.get('content_type','article')} | "
            f"时长:{a.get('read_time_display','')} | "
            f"{status}{flag_str}"
        )
    return '\n'.join(lines)


# ── LLM-Generated Deep User Profile (Plan 1) ──

_llm_user_profile_cache = None
_llm_user_profile_event_count = 0
_llm_user_profile_timestamp = 0


def _get_or_generate_llm_user_profile(api_config, force=False):
    """Return cached LLM-generated user interest profile, regenerating if stale.

    Cache invalidation: event count changed OR 6 hours elapsed since generation.
    Returns None if user has insufficient activity or LLM call fails.
    """
    global _llm_user_profile_cache, _llm_user_profile_event_count, _llm_user_profile_timestamp

    event_count = database.get_event_count()
    now = time.time()

    if not force and _llm_user_profile_cache is not None:
        if event_count == _llm_user_profile_event_count and (now - _llm_user_profile_timestamp) < 21600:
            return _llm_user_profile_cache

    profile_data = database.get_user_profile_data_for_llm()
    if not profile_data:
        _llm_user_profile_cache = None
        return None

    profile_text = _generate_llm_user_profile_text(api_config, profile_data)
    if profile_text:
        _llm_user_profile_cache = profile_text
        _llm_user_profile_event_count = event_count
        _llm_user_profile_timestamp = now
        logger.info('LLM user profile regenerated successfully')
    else:
        logger.warning('LLM user profile generation failed, keeping previous cache if any')

    return _llm_user_profile_cache


def _generate_llm_user_profile_text(api_config, data):
    """Call LLM to synthesize rich behavior data into a natural-language user profile.

    Args:
        api_config: LLM API configuration dict
        data: output from database.get_user_profile_data_for_llm()

    Returns: natural-language profile text (Chinese), or None on failure
    """
    # Build a structured summary of the raw data for the LLM prompt
    lines = []

    lines.append(f"用户总互动次数：{data['event_count']} 次\n")

    # Recently read
    if data['recently_read']:
        lines.append('## 最近阅读/打开的文章')
        for r in data['recently_read'][:20]:
            action_label = '已读完' if r['action'] == 'read_done' else '打开过'
            lines.append(f'- [{r["label"]}] {r["title"]}（{r["duration"]}，{action_label}，{r["type"]}）')
    else:
        lines.append('## 最近阅读：暂无')

    # Recently dismissed
    if data['recently_dismissed']:
        lines.append('\n## 最近忽略/不感兴趣的文章')
        for r in data['recently_dismissed'][:10]:
            lines.append(f'- [{r["label"]}] {r["title"]}')
    else:
        lines.append('\n## 忽略记录：暂无')

    # Recently snoozed
    if data['recently_snoozed']:
        lines.append('\n## 最近延后阅读的文章')
        for r in data['recently_snoozed'][:5]:
            lines.append(f'- [{r["label"]}] {r["title"]}')

    # Per-label summary
    if data['label_summary']:
        lines.append('\n## 各标签互动统计（不含通知）')
        for s in data['label_summary']:
            parts = []
            if s['read']:
                parts.append(f'读完{s["read"]}篇')
            if s['opened']:
                parts.append(f'打开{s["opened"]}篇')
            if s['dismissed']:
                parts.append(f'忽略{s["dismissed"]}篇')
            if s['snoozed']:
                parts.append(f'延后{s["snoozed"]}篇')
            lines.append(f'- {s["label"]}：{", ".join(parts)}')

    # Reading time preferences
    if data['reading_time_preferences']:
        lines.append('\n## 阅读时长偏好')
        for t in data['reading_time_preferences']:
            lines.append(f'- {t["minutes"]}分钟的文章：互动{t["count"]}次')

    # Content type preferences
    if data['content_type_preferences']:
        lines.append('\n## 内容类型偏好')
        for t in data['content_type_preferences']:
            lines.append(f'- {t["type"]}：互动{t["count"]}次')

    # Unread by label
    if data['unread_by_label']:
        lines.append('\n## 待读文章分布')
        for u in data['unread_by_label']:
            lines.append(f'- {u["label"]}：{u["count"]}篇待读')

    data_text = '\n'.join(lines)

    profile_prompt = (
        "You are an expert user behavior analyst. Based on the following reading history data, "
        "write a concise user interest profile in Chinese (150-250 words).\n\n"
        "The profile should capture:\n"
        "1. Core interests and favorite topics — be specific, not just label names. "
        "If they read 5 articles about LLM architecture, say 'deeply interested in LLM architecture', not just 'likes tech'.\n"
        "2. Content they actively avoid or dislike — what topics do they dismiss?\n"
        "3. Reading habits — preferred duration, article vs video, any patterns.\n"
        "4. Cross-topic patterns — e.g., only reads hands-on tutorials, skips theoretical papers.\n"
        "5. Any notable recent shifts or emerging interests.\n\n"
        "IMPORTANT RULES:\n"
        "- Write in natural, flowing Chinese. This profile will be read by another AI assistant.\n"
        "- Be specific and insightful. Don't just restate the data — synthesize and interpret.\n"
        "- Keep it between 150-250 words. No bullet points — use continuous prose.\n"
        "- If data is sparse on some dimensions, focus on what's available rather than noting gaps.\n"
        "- The tone should be informative and analytical, like a user researcher's notes.\n\n"
        "User behavior data:\n" + data_text
    )

    result = _call_llm_query(api_config, profile_prompt, '', max_tokens=500)
    if result:
        result = result.strip()
        if len(result) < 30:
            logger.warning('LLM profile too short, discarding')
            return None
    return result


# ── LLM Re-Ranking (Plan 2) ──

def _llm_rerank_candidates(api_config, user_profile_text, question, candidates,
                           max_candidates=25):
    """Use LLM to re-rank candidate articles based on deep user understanding.

    Args:
        api_config: LLM API configuration
        user_profile_text: natural-language user profile from _get_or_generate_llm_user_profile
        question: the user's current question/request
        candidates: list of article dicts from get_recommendable_articles
        max_candidates: max candidates to send to LLM (default 25)

    Returns: reordered list of article dicts, or original order if LLM fails
    """
    if len(candidates) < 5:
        return candidates

    # Format candidates for LLM with richer context
    top_candidates = candidates[:max_candidates]
    candidate_lines = []
    for i, a in enumerate(top_candidates):
        score = a.get('_score', '?')
        status = '已读' if a.get('is_read') else '未读'
        flags = []
        if a.get('dismiss_count'):
            flags.append(f'忽略{a["dismiss_count"]}次')
        if a.get('snooze_until'):
            flags.append(f'延后至{a["snooze_until"][:10]}')
        if a.get('_opened_unfinished'):
            flags.append('上次未看完')
        flag_str = (' [' + ', '.join(flags) + ']') if flags else ''

        reasons = a.get('_reasons', [])
        reason_text = ''
        if reasons:
            reason_text = ' | 推荐理由：' + '；'.join(r.get('text', '') for r in reasons)

        candidate_lines.append(
            f"[ID:{a.get('id', '')}] score={score} 《{a.get('title', '')}》 | "
            f"标签:{a.get('label', '')} | "
            f"时长:{a.get('read_time_display', '')} | "
            f"类型:{a.get('content_type', 'article')} | "
            f"{status}{flag_str}{reason_text}"
        )

    candidates_text = '\n'.join(
        f'{i+1}. {line}' for i, line in enumerate(candidate_lines)
    )

    profile_section = ''
    if user_profile_text:
        profile_section = (
            '## 用户画像（AI生成的深度分析）\n'
            f'{user_profile_text}\n\n'
        )

    rerank_prompt = (
        "You are a personalized recommendation engine for a Chinese reading app.\n"
        "Your task: re-rank candidate articles based on deep understanding of this specific user.\n\n"
        + profile_section +
        f"## 用户当前请求\n{question}\n\n"
        f"## 候选文章（已按基础算法排序，共{len(top_candidates)}篇）\n{candidates_text}\n\n"
        "## 重排序规则\n"
        "Deeply consider the user profile above and re-rank from most to least relevant:\n"
        "1. **Topic precision**: Go beyond label matching. If the user loves LLM architectures, "
        "a '专业前沿' article about GPT-5 should rank higher than a '专业前沿' article about Excel tips.\n"
        "2. **Avoid known dislikes**: If the user consistently dismisses certain topics, deprioritize those.\n"
        "3. **Respect context**: If the user mentioned available time or specific interests, factor that in.\n"
        "4. **Diversity**: Don't cluster all the same-topic articles at the top — spread different angles.\n"
        "5. **Interrupted interest**: Articles the user opened but didn't finish deserve priority.\n"
        "6. **Snoozed articles**: Don't exclude them, but don't over-prioritize either.\n\n"
        "Output ONLY the re-ranked article IDs in order, one per line. No explanation, no markdown.\n"
        "Example output format:\n"
        "15\n22\n8\n30\n19\n..."
    )

    result = _call_llm_query(api_config, rerank_prompt, candidates_text, max_tokens=400)
    if not result:
        logger.info('LLM rerank failed, keeping original order')
        return candidates

    # Parse re-ranked IDs
    reranked_ids = []
    for line in result.strip().split('\n'):
        line = line.strip()
        try:
            aid = int(line)
            reranked_ids.append(aid)
        except ValueError:
            # Try to extract ID from various formats
            import re as _re
            m = _re.search(r'\b(\d+)\b', line)
            if m:
                reranked_ids.append(int(m.group(1)))

    if len(reranked_ids) < 3:
        logger.info(f'LLM rerank produced too few IDs ({len(reranked_ids)}), keeping original order')
        return candidates

    # Build lookup from original candidates
    candidate_map = {a['id']: a for a in candidates}

    # Reorder: include all candidates, LLM-ranked first, remaining in original order
    ranked = []
    seen = set()
    for aid in reranked_ids:
        if aid in candidate_map and aid not in seen:
            ranked.append(candidate_map[aid])
            seen.add(aid)

    # Append any candidates the LLM didn't mention
    for a in candidates:
        if a['id'] not in seen:
            ranked.append(a)

    logger.info(f'LLM rerank: {len(ranked)} candidates, top 5 IDs: {[a["id"] for a in ranked[:5]]}')
    return ranked


def _call_llm_query(api_config, system_prompt, user_prompt, max_tokens=600):
    """Make a single LLM call for query pipeline."""
    api_base = (api_config.get('api_base', '') or 'https://api.deepseek.com/v1').rstrip('/')
    api_key = api_config.get('api_key', '')
    if not api_key or not api_base:
        return None
    endpoint = api_base + '/chat/completions'
    try:
        resp = requests.post(
            endpoint,
            headers={
                'Authorization': 'Bearer ' + api_key,
                'Content-Type': 'application/json',
            },
            json={
                'model': api_config.get('model', 'deepseek-chat'),
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt},
                ],
                'max_tokens': max_tokens,
                'temperature': 0.1,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning(f'LLM API returned {resp.status_code}: {resp.text[:200]}')
            return None
        data = resp.json()
        if 'choices' in data and len(data['choices']) > 0:
            return data['choices'][0]['message']['content'].strip()
        logger.warning(f'LLM query API error: {data}')
        return None
    except Exception as e:
        logger.warning(f'LLM query call failed: {e}')
        return None


def _build_context_hint(history, current_question, max_rounds=5, max_content_len=200):
    """Build a concise conversation context hint from recent history.

    Extracts user intents from prior turns and frames them so the LLM can
    understand follow-up requests like '换一批' or '只要时政' in context.

    Args:
        history: list of {role, content} dicts, ordered oldest→newest
        current_question: the user's latest question
        max_rounds: max number of previous rounds to include
        max_content_len: max chars per message stored in context

    Returns a string to inject into LLM prompts, or empty string.
    """
    if not history or not isinstance(history, list) or len(history) < 2:
        return ''

    # Keep only user + assistant roles, and only the last N rounds
    valid = [h for h in history if isinstance(h, dict) and h.get('role') in ('user', 'assistant')]
    if len(valid) < 2:
        return ''

    # Truncate to last N*2 messages (N rounds, each round = user + assistant)
    trimmed = valid[-(max_rounds * 2):]

    # Build a compact summary
    lines = ['## 对话上下文（多轮对话记忆）']
    lines.append('以下是与用户的对话历史。请结合上下文理解当前请求。\n')

    for i, msg in enumerate(trimmed):
        role_label = '用户' if msg['role'] == 'user' else 'AI助手'
        content = (msg.get('content', '') or '')[:max_content_len]
        if not content.strip():
            continue
        lines.append(f'{role_label}：{content}')

    lines.append(f'\n当前用户最新消息：「{current_question}」\n')
    lines.append(
        '【重要】结合对话历史理解用户意图：\n'
        '- 「换一批」意味着保持上一轮的筛选条件（标签/话题/时长等），推荐不同的文章\n'
        '- 「只要X类」「只看X」「换成X」意味着继承上一轮的话题范围，调整过滤条件\n'
        '- 如果当前消息是明确的新话题，则以上下文为辅、当前消息为主\n'
        '- 如果上一轮是推荐请求，本轮是跟进追问，应在相同约束下继续服务\n'
    )

    result = '\n'.join(lines)
    MAX_TOTAL_CHARS = 2000
    if len(result) > MAX_TOTAL_CHARS:
        # Truncate individual messages first, then hard-cut if still too long
        result = result[:MAX_TOTAL_CHARS]
        # Try to cut at a clean boundary (newline)
        last_nl = result.rfind('\n')
        if last_nl > MAX_TOTAL_CHARS * 0.7:
            result = result[:last_nl]
        result += '\n（对话历史较长，已截断至最近的关键上下文）'
    return result


def _classify_query_intent(question):
    """Classify user question intent: recommendation | stats | search."""
    # 强推荐：明确要「挑一篇 / 读哪篇」
    if any(
        p in question
        for p in (
            '推荐',
            '帮我选',
            '看哪篇',
            '挑一篇',
            '找一篇',
            '帮我挑',
            '该读',
            '应该读',
            '换一批',
            '换一篇',
            '换一个',
            '再推荐',
            '还有吗',
            '帮我想',
            '帮我推荐',
        )
    ):
        return 'recommendation'

    stats_patterns = (
        '多少',
        '几个',
        '统计',
        '数量',
        '总共',
        '有哪些标签',
        '分类',
        '分布',
        '占比',
    )
    if any(p in question for p in stats_patterns):
        return 'stats'

    # 列举/筛选文库（有哪些篇、未读有哪些）→ 走检索/SQL，不要进推荐快路径
    if '推荐' not in question:
        if re.search(r'(有哪些|有什么).{0,14}(文章|未读|已读)', question):
            return 'search'
        if re.search(r'(未读|已读).{0,10}(哪几|哪些|有什么|有哪些)', question):
            return 'search'
        if ('列出' in question or '显示' in question or '查看' in question) and (
            '文章' in question or '未读' in question or '已读' in question or '链接' in question
        ):
            return 'search'

    # 弱推荐：如「有什么好看的」「有哪些方向」——没有明确枚举文库时仍偏「帮我选」
    # 但通知/提醒类关键词 → 走检索/SQL，推荐快路径默认排除通知
    notif_kw = ('通知', '提醒', 'deadline', 'DDL', 'ddl', '截止', '报名', '错过', '时间节点')
    if any(k in question for k in notif_kw):
        return 'search'
    if any(p in question for p in ('有什么', '有哪些', '介绍', '看什么')):
        return 'recommendation'

    return 'search'


# Per-session tracking of recommended article IDs to avoid repetition
_recommended_in_session = set()
# Per-session topic exclusions (user said "不要再给我推X")
_excluded_topics = set()

def _extract_excluded_topic(question):
    """Extract topic from negative feedback patterns like '不要再给我推X/别推X'."""
    m = re.search(r'(?:不要再|别再?|不要).{0,6}(?:推|推荐).{0,6}(?:了)?(.{1,6}?)(?:的|了|内容|文章|吧|啦|啊|哈|哦|～|~|\s|$)', question)
    if m:
        return m.group(1).strip()
    return None

def _llm_recommend(question, api_config, stats, history=None):
    """Recommendation fast path: scored retrieval → LLM rerank → LLM natural answer.

    Plan 2 enhancement: fetches top 25 candidates, applies LLM re-ranking with
    deep user profile, then presents with rich personalization context.
    """
    # Fetch more candidates for LLM re-ranking
    # Include notifications when user explicitly asks about them
    notif_kw = ('通知', '提醒', 'deadline', 'DDL', 'ddl', '截止', '报名', '错过', '时间节点')
    exclude_notif = not any(k in question for k in notif_kw)
    rows = database.get_recommendable_articles(limit=25, exclude_notification=exclude_notif)

    # Dedup: move already-recommended-in-session articles to end of list
    global _recommended_in_session
    if _recommended_in_session:
        fresh = [r for r in rows if r['id'] not in _recommended_in_session]
        seen = [r for r in rows if r['id'] in _recommended_in_session]
        rows = fresh + seen

    # Topic exclusion: process "不要再给我推X" patterns
    global _excluded_topics
    excluded_topic = _extract_excluded_topic(question)
    if excluded_topic:
        _excluded_topics.add(excluded_topic)
    # Filter out articles matching any excluded topic (check title + label)
    if _excluded_topics:
        rows = [r for r in rows if not any(
            et in (r.get('title', '') + r.get('label', ''))
            for et in _excluded_topics
        )]

    if not rows:
        return _empty_result(question, api_config, stats, history=history)

    # ── Expand candidate pool with label / keyword matches BEFORE re-rank ──
    existing_ids = {r['id'] for r in rows}

    # Check if user asked for a specific topic
    known_labels = set(stats.get('by_label', {}).keys()) | {'通知', '文艺', '专业前沿', '时政', '娱乐', '攻略'}
    topic_label = None
    for label in known_labels:
        if label in question:
            topic_label = label
            break

    # When user explicitly asks for a label, also fetch articles with that label
    if topic_label:
        labeled_articles, _ = database.get_articles(filter=topic_label, limit=10)
        for la in labeled_articles:
            if la['id'] not in existing_ids:
                la['_score'] = 99
                rows.append(la)
                existing_ids.add(la['id'])

    # Keyword search fallback: when no label matches, search article titles for
    # topic keywords mentioned in the query (e.g. "推荐一篇Python入门" → find "Python")
    if not topic_label:
        raw_keywords = re.findall(r'[一-鿿]{2,6}|[a-zA-Z]{2,}', question)
        stop_words = {
            '推荐', '一篇', '帮我', '我想', '有没有', '最近', '关于', '内容',
            '文章', '什么', '怎么', '可以', '应该', '这个', '那个', '现在',
            '比较', '还是', '一个', '一些', '一下', '有点', '哪些', '不知道',
            '有没有', '能不能', '是不是', '可不可以', '感兴趣', '了解', '想看',
            '看点', '介绍', '分钟', '小时', '午休', '上课', '晚上', '周末',
            '今天', '明天', '不要', '已经', '随便', '就是', '喜欢', '觉得',
            '适合', '轻松', '治愈', '搞笑', '深度', '硬核', '开心',
        }
        keywords = [kw for kw in raw_keywords if kw not in stop_words and kw not in known_labels]
        if keywords:
            for kw in keywords[:3]:
                matched, _ = database.get_articles(search=kw, limit=8)
                for ma in matched:
                    if ma['id'] not in existing_ids:
                        ma['_score'] = 90
                        rows.append(ma)
                        existing_ids.add(ma['id'])

    # Randomize for "随机" queries
    if '随机' in question:
        import random
        random.shuffle(rows)

    # ── Plan 1 + 2: LLM Deep User Profile + Re-Ranking ──
    llm_profile = _get_or_generate_llm_user_profile(api_config)
    if llm_profile and len(rows) >= 5:
        rows = _llm_rerank_candidates(api_config, llm_profile, question, rows)
        logger.info(f'LLM re-rank applied for question: {question[:50]}')

    # Use top 10 for presentation
    top_rows = rows[:10]
    articles_context = _format_articles_for_llm(top_rows)

    topic_hint = f'\n用户提到了「{topic_label}」相关话题，优先推荐这个标签的文章。' if topic_label else ''
    exclusion_hint = ''
    if _excluded_topics:
        exclusion_hint = f'\n用户已明确排除以下话题：{", ".join(_excluded_topics)}。绝对不要推荐包含这些关键词的文章。在回答中自然地提及已排除这些话题。'
    context_hint = _build_context_hint(history, question)

    # Build user profile section for the prompt
    profile_section = ''
    if llm_profile:
        profile_section = (
            '\n## 用户深度画像（AI 分析生成）\n'
            f'{llm_profile}\n'
            '\n请基于以上画像理解用户，推荐时自然融入对用户兴趣的洞察。\n'
        )
    else:
        # Fallback: simple label-based preference hint
        label_scores_in_results = {}
        for r in top_rows:
            lbl = r.get('label', '')
            if lbl and lbl != '未分类':
                label_scores_in_results[lbl] = label_scores_in_results.get(lbl, 0) + r.get('_score', 0)
        top_scored_labels = sorted(label_scores_in_results.items(), key=lambda x: -x[1])[:3]
        if top_scored_labels:
            pref_labels = '、'.join(f'{lbl}' for lbl, _ in top_scored_labels)
            profile_section = (
                f'\n【用户阅读偏好】根据历史行为推断，用户最感兴趣的标签依次为：{pref_labels}。'
                f'请优先推荐这些标签的文章，并在推荐理由中自然提及用户的阅读习惯。\n'
            )

    answer_prompt = (
        "You are '稍后看', a smart Chinese reading assistant.\n\n"
        f"User asked: \"{question}\"\n"
        f"{context_hint}\n"
        f"{profile_section}\n"
        f"{topic_hint}\n"
        f"{exclusion_hint}\n"
        f"Candidate articles (scored by priority):\n{articles_context}\n\n"
        "GUIDELINES:\n"
        "- Present 2-4 distinct options using [OPTION:id] format.\n"
        "- Each option: [OPTION:id] 【时长 · 标签】**标题** | 一句推荐理由\n"
        "- If user mentioned a time constraint, prefer articles that fit.\n"
        "- If user mentioned a topic, prioritize matching labels.\n"
        "- Tone: warm and conversational, like a thoughtful friend sharing a good find.\n"
        "  Use natural Chinese phrasing — 呢 is fine when it softens the tone.\n"
        "  Never sound clinical or robotic. Avoid phrases like '评分最高', '候选文章', '各有特点'.\n"
        "  Instead, say things like '这篇刚好适合你' or '时间不长，可以趁休息翻一翻'.\n"
        "- Start with a gentle one-line summary that acknowledges the user's context.\n"
        "- End with a warm closing, e.g. '挑一篇感兴趣的随时开始～' or '想了解哪篇可以问我。'\n"
        "- Never add up reading times. Never recommend 通知 as casual reading.\n"
        "- If an article is snoozed, note it: '（已延后，可取消）'\n"
        "- Prioritize higher-scored articles (score shown in context).\n"
        "- FACTUALITY: Recommendation reasons must use only facts visible in the candidate lines "
        "(title, label, time, summary/snippet, score). Do not invent deadlines, links, or topics not shown."
    )

    answer = _call_llm_query(api_config, answer_prompt, articles_context, max_tokens=600)
    if not answer:
        answer = f'帮你看了看，推荐这几篇：\n' + '\n'.join(f'• {r["title"][:30]}' for r in top_rows[:5]) + '\n\n想了解哪篇可以问我～'

    # Track which articles were recommended to avoid repetition
    rec_ids = re.findall(r'\[OPTION:(\d+)\]', answer)
    for rid in rec_ids:
        _recommended_in_session.add(int(rid))

    return {
        'answer': answer,
        'articles': top_rows,
        'query_type': 'llm_recommend',
        'total_matches': len(rows),
    }


def _llm_stats(question, api_config, stats, history=None):
    """Stats fast path: LLM answers from statistics data directly."""
    stats_context = (
        f"Total: {stats['total']} articles. "
        f"Unread: {stats['unread']}. "
        f"Read: {stats['read']}. "
        f"Label distribution: {stats['by_label']}."
    )
    context_hint = _build_context_hint(history, question)
    answer_prompt = (
        "You are '稍后看', a smart Chinese reading assistant.\n\n"
        f"Library stats: {stats_context}\n\n"
        f"{context_hint}\n"
        f"User asked: \"{question}\"\n\n"
        "Answer naturally based on the stats data. "
        "Be direct and helpful. Mention specific numbers. "
        "If user asks about labels, list the available labels with counts. "
        "No [OPTION:] format needed for stats answers."
    )
    answer = _call_llm_query(api_config, answer_prompt, question, max_tokens=300)
    if not answer:
        label_list = ', '.join(f'{k}({v})' for k, v in stats['by_label'].items())
        answer = f'知识库共 {stats["total"]} 篇，未读 {stats["unread"]} 篇。分类分布：{label_list}。'
    return {
        'answer': answer,
        'articles': [],
        'query_type': 'llm_stats',
        'total_matches': 0,
    }


def _empty_result(question, api_config, stats, history=None):
    """Generate a helpful response when no articles match."""
    fallback_rows = database.get_recommendable_articles(limit=5)
    if not fallback_rows:
        return {
            'answer': '你的知识库里暂时还没有文章呢，先收藏几篇链接吧～',
            'articles': [],
            'query_type': 'llm',
            'total_matches': 0,
        }
    alt_context = _format_articles_for_llm(fallback_rows)
    context_hint = _build_context_hint(history, question)
    llm_profile_alt = _get_or_generate_llm_user_profile(api_config)
    profile_section_alt = ''
    if llm_profile_alt:
        profile_section_alt = f'\n## 用户深度画像\n{llm_profile_alt}\n'
    answer_prompt = (
        "You are '稍后看', a smart Chinese reading assistant.\n\n"
        f"{context_hint}\n"
        f"{profile_section_alt}\n"
        f"User asked: \"{question}\" — no articles exactly matched, but alternatives exist.\n\n"
        f"Alternative articles:\n{alt_context}\n\n"
        "Suggest 2-3 alternatives using [OPTION:id] format. "
        "Explain that no exact match was found but these might interest them. "
        "Be warm and conversational. End with '挑一篇感兴趣的随时开始，或者告诉我你想找什么类型～'\n"
        "FACTUALITY: Reasons must come only from the alternative article fields shown — no invented facts."
    )
    answer = _call_llm_query(api_config, answer_prompt, question, max_tokens=400)
    if not answer:
        answer = f'没有找到与「{question}」完全匹配的文章，但这些或许你会感兴趣。'
    rec_ids = re.findall(r'\[OPTION:(\d+)\]', answer)
    for rid in rec_ids:
        _recommended_in_session.add(int(rid))
    return {
        'answer': answer,
        'articles': fallback_rows,
        'query_type': 'llm',
        'total_matches': 0,
    }


def _llm_query(question, api_config, stats, history=None):
    """Use LLM to answer natural language questions about the database."""
    intent = _classify_query_intent(question)

    # Fast path for recommendation intent: skip SQL, use scored retrieval directly
    if intent == 'recommendation':
        result = _llm_recommend(question, api_config, stats, history=history)
        if result:
            return result
        # Fall through to full pipeline if recommend fails

    # Fast path for stats intent: LLM answers from stats data
    if intent == 'stats':
        result = _llm_stats(question, api_config, stats, history=history)
        if result:
            return result

    # Full search pipeline: LLM → SQL → execute → score → LLM answer
    schema_desc = f"""Table articles:
  id INTEGER PK, url TEXT UNIQUE, title TEXT, author TEXT, source TEXT,
  full_content TEXT (complete article body), content_preview TEXT,
  summary TEXT, summary_mode TEXT,
  label TEXT (AI-assigned category), label_confidence INTEGER (0-100),
  content_type TEXT (video|image|article|other),
  read_time_min INTEGER, read_time_display TEXT,
  is_read INTEGER (0=unread, 1=read), read_at TEXT,
  label_confirmed INTEGER, dismiss_count INTEGER DEFAULT 0,
  snooze_until TEXT (NULL or datetime, if set and in future → exclude from recommendations),
  created_at TEXT (YYYY-MM-DD HH:MM:SS)

Table article_user_labels (JOIN via aul.article_id = a.id):
  article_id INTEGER FK, label_name TEXT — NOTE: column is label_name NOT label

Table user_events:
  article_id INTEGER FK, event_type TEXT ('opened'|'read_done'|'dismiss'|'snooze'),
  payload TEXT JSON, created_at TEXT

Current stats: {stats['total']} total articles, {stats['unread']} unread, {stats['read']} read.
Label distribution: {stats['by_label']}"""

    # Stage 1: Generate SQL
    sql_prompt = (
        "You are a SQL expert for a Chinese reading app '稍后看'. "
        "Here is the SQLite schema:\n\n" + schema_desc + "\n\n"
        "Write a single SELECT query to answer the user's question. Rules:\n"
        "- ONLY SELECT statements. Never INSERT/UPDATE/DELETE/DROP.\n"
        "- Always SELECT a.* (all article columns) — never hand-pick columns.\n"
        "- Return ONLY the raw SQL, no explanation, no markdown formatting.\n"
        "- For label searches, check both articles.label AND article_user_labels.label_name via JOIN.\n"
        "- For time-based queries (yesterday, last week): compare created_at using datetime().\n"
        "- For '没看完': find articles with opened events but no read_done event.\n"
        "- Limit results to 15 (the system will re-score and sort afterwards).\n"
        "- Label values are Chinese category names (e.g. 时政, 攻略). Match them literally in SQL.\n"
        "- Use table alias `a` for articles. After JOIN, use SELECT DISTINCT a.* if rows may duplicate.\n"
        "\n"
        "GOLDEN SQL PATTERNS (adapt to the user's question; do not copy unrelated filters):\n"
        "-- Unread, exclude 通知, newest first\n"
        "SELECT a.* FROM articles a WHERE a.is_read = 0 AND IFNULL(a.label,'') != '通知' "
        "ORDER BY a.created_at DESC LIMIT 15\n"
        "\n"
        "-- Filter by label (articles.label OR user override label_name)\n"
        "SELECT DISTINCT a.* FROM articles a "
        "LEFT JOIN article_user_labels aul ON aul.article_id = a.id "
        "WHERE a.label = '时政' OR IFNULL(aul.label_name,'') = '时政' "
        "ORDER BY a.created_at DESC LIMIT 15\n"
        "\n"
        "-- ~10 minutes available: read_time_min <= round(minutes * 1.4)\n"
        "SELECT a.* FROM articles a WHERE a.is_read = 0 AND IFNULL(a.label,'') != '通知' "
        "AND a.read_time_min <= 14 ORDER BY a.created_at DESC LIMIT 10\n"
        "\n"
        "-- User asks about 通知/提醒/deadlines: MUST include 通知, do NOT exclude\n"
        "SELECT a.* FROM articles a WHERE a.label = '通知' AND a.is_read = 0 "
        "ORDER BY a.created_at DESC LIMIT 15\n"
        "\n"
        "-- Opened but not finished (no read_done event)\n"
        "SELECT DISTINCT a.* FROM articles a "
        "JOIN user_events e1 ON e1.article_id = a.id AND e1.event_type = 'opened' "
        "WHERE NOT EXISTS (SELECT 1 FROM user_events e2 WHERE e2.article_id = a.id AND e2.event_type = 'read_done') "
        "ORDER BY a.created_at DESC LIMIT 15\n"
        "\n"
        "FOLLOW-UP TOPIC REQUESTS (CRITICAL):\n"
        "When the user says things like '我想要X类的' / '有X方面的吗' / '只看X' / '有没有X' /\n"
        "'换成X' / '换一批X' / '时政类' / '科技相关' — these are TOPIC FILTERS, NOT literal text searches.\n"
        "→ Extract the label name and filter: WHERE label = 'X' OR aul.label_name = 'X'\n"
        "→ Do NOT use LIKE '%我想要X类的%' — this will never match.\n"
        "\n"
        "SNOOZE: Do NOT hard-exclude snoozed articles. Include them — the downstream scorer handles priority.\n"
        "\n"
        "RECOMMENDATION FILTERING (when user asks to recommend or mentions available time):\n"
        "0. Exclude 通知 from casual recommendations: WHERE label != '通知'.\n"
        "   Only include 通知 when user explicitly asks about reminders/deadlines/提醒.\n"
        "1. Prefer unread: is_read = 0 (but don't hard-exclude read articles).\n"
        "2. If user specifies time: filter read_time_min <= time * 1.4, limit 10.\n"
        "3. If user specifies TOPIC: filter by topic. LIMIT 15.\n"
        "4. If NO topic, NO time: just fetch is_read=0 AND label!='通知' LIMIT 15.\n"
        "5. Convert time units: 小时→×60, 分钟/分→×1, 半小时=30.\n"
        "6. SIMPLE ORDER BY is fine — the system re-scores everything. Use created_at DESC as default."
    )

    context_hint = _build_context_hint(history, question)
    user_prompt = (
        f"{context_hint}\n"
        f"User question: {question}"
    ) if context_hint else f"User question: {question}"
    sql_raw = _call_llm_query(api_config, sql_prompt, user_prompt, max_tokens=400)
    if not sql_raw:
        return None

    sql = _sanitize_sql(sql_raw)
    logger.info(f'LLM generated SQL: {sql}')

    if not _is_safe_sql(sql):
        logger.warning(f'Unsafe SQL blocked: {sql}')
        return None

    # Execute SQL
    try:
        rows = database.execute_raw(sql)
    except Exception as e:
        logger.warning(f'SQL execution failed: {e}, retrying with error feedback')
        retry_prompt = (
            f"Schema reminder:\n{schema_desc}\n\n"
            f"The previous SQL gave this SQLite error: {e}\n"
            "Fix the error using the correct column names from the schema above.\n"
            "Return ONLY the corrected SQL, no explanation."
        )
        sql_raw2 = _call_llm_query(api_config, sql_prompt, retry_prompt, max_tokens=400)
        if sql_raw2:
            sql2 = _sanitize_sql(sql_raw2)
            if _is_safe_sql(sql2):
                try:
                    rows = database.execute_raw(sql2)
                except Exception as e2:
                    logger.warning(f'Retry SQL also failed: {e2}')
                    return None
            else:
                return None
        else:
            return None

    # ── Score & Sort: apply unified scoring to all candidates ──
    rows = database.compute_article_scores(list(rows))

    # Stage 2: Generate natural language response
    if not rows:
        # No articles matched — still use LLM to give a helpful response
        # Pass the article stats so LLM can suggest available labels/topics
        stats_context = (
            f"Total articles: {stats['total']}, unread: {stats['unread']}. "
            f"Available labels: {list(stats['by_label'].keys())}. "
            f"The user asked '{question}' but NO articles matched the query."
        )
        context_hint_empty = _build_context_hint(history, question)
        llm_profile_empty = _get_or_generate_llm_user_profile(api_config)
        profile_section_empty = ''
        if llm_profile_empty:
            profile_section_empty = (
                '\n## 用户深度画像\n'
                f'{llm_profile_empty}\n'
            )
        empty_answer_prompt = (
            "You are '稍后看', a smart Chinese reading assistant.\n\n"
            f"{context_hint_empty}\n"
            f"{profile_section_empty}\n"
            f"Context: {stats_context}\n\n"
            "The user's query returned ZERO matching articles. Respond helpfully:\n"
            "- If the user asked for a specific label/topic that doesn't exist, tell them:\n"
            "  '暂未收录「X」类文章。当前分类包括：{available labels}'\n"
            "- Suggest 2-3 articles from other topics, using the format:\n"
            "  [OPTION:id] 【时长 · 标签】**标题** | 推荐理由\n"
            "- If it was a literal search (not a topic filter), briefly explain no match was found\n"
            "  and suggest browsing by available labels.\n"
            "- Tone: warm and natural. No clinical language. Avoid 呀/呢/哦 overuse but be conversational.\n"
            "- Never just say '没有找到' and stop — always offer alternatives.\n"
            "- Never add up reading times. Keep the response brief and direct.\n"
            "- Do NOT recommend 通知 articles as reading material.\n"
            "- FACTUALITY: When listing [OPTION:] items, reasons must use only title/label/time/summary from the "
            "provided context — never fabricate deadlines or content."
        )
        # Re-fetch top-scored articles as alternatives (excludes 通知 by default)
        fallback_rows = database.get_recommendable_articles(limit=5)
        if fallback_rows:
            alt_context = _format_articles_for_llm(fallback_rows)
            empty_answer_prompt += (
                "\n\nHere are some alternative articles you can recommend:\n" + alt_context
            )
        answer = _call_llm_query(api_config, empty_answer_prompt, question, max_tokens=500)
        if not answer:
            answer = f'没有找到与「{question}」完全匹配的文章。你可以试试浏览已有的标签：{", ".join(stats["by_label"].keys())}。'
        rec_ids = re.findall(r'\[OPTION:(\d+)\]', answer)
        for rid in rec_ids:
            _recommended_in_session.add(int(rid))
        return {
            'answer': answer,
            'articles': fallback_rows or [],
            'query_type': 'llm',
            'total_matches': 0,
        }

    articles_context = _format_articles_for_llm(rows)
    llm_profile_main = _get_or_generate_llm_user_profile(api_config)
    profile_section_main = ''
    if llm_profile_main:
        profile_section_main = (
            '\n## 用户深度画像（AI 分析生成）\n'
            f'{llm_profile_main}\n'
            '\n请基于以上画像理解用户，推荐时自然融入对用户兴趣的洞察。\n'
        )
    answer_prompt = (
        "You are '稍后看', a smart Chinese reading assistant. You have access to the user's "
        "article library and understand their preferences. Use your judgment flexibly.\n\n"
        f"{context_hint}\n"
        f"{profile_section_main}\n"
        f"User's question: \"{question}\"\n\n"
        f"Candidate articles in the library:\n{articles_context}\n\n"
        "Each article shows its status — pay attention to '已延后至...' flags.\n\n"
        "GUIDELINES (not rigid rules — use common sense):\n\n"
        "- The goal is to help the user make the best use of their time and interest.\n"
        "- Present 2-4 distinct options as recommendations. Each option should be a different article.\n"
        "- If one article is a near-perfect match (time + topic), still offer 1-2 alternatives.\n"
        "- If the user has a long time window: group 2-3 articles as one option (a mini reading list).\n"
        "- Each option should include the article ID and a short reason.\n"
        "\n"
        "FACTUALITY (IMPORTANT):\n"
        "- Every reason must be grounded in the candidate block (title, label, read time, summary/snippet, status).\n"
        "- Do not invent dates, URLs, authors, or claims not present in that block.\n"
        "\n"
        "TONE (IMPORTANT):\n"
        "- Write like a thoughtful friend sharing a good find — warm, natural, conversational.\n"
        "- Use natural Chinese conversational phrasing. 呢 / 吧 / 哦 are fine in moderation.\n"
        "- Never sound clinical, robotic, or like a search engine.\n"
        "- Forbidden phrasing: '评分最高', '候选文章', '各有特点', '需注意时长'.\n"
        "  Instead, be personal: '这篇刚好适合你', '时间不长，趁休息翻一翻', '另外两篇也不错但篇幅偏长'.\n"
        "- Avoid cold data dumps. Weave time constraints and topic preferences into the conversation naturally.\n"
        "- Start with a gentle one-line summary that acknowledges the user's context\n"
        "  (e.g. '趁休息时间帮你翻了翻，找到这几篇～').\n"
        "- End warmly, e.g. '挑一篇感兴趣的随时开始' or '想先看哪篇？'.\n"
        "- Never feel like a recommendation engine. Feel like a well-read friend.\n"
        "\n"
        "通知 ARTICLES (IMPORTANT):\n"
        "- 通知 articles are time-sensitive alerts — NOT casual reading material.\n"
        "- When the user explicitly asks about 通知/提醒/deadlines/DDL/截止日期:\n"
        "  → Prioritize 通知 articles. List them with their deadlines prominently displayed.\n"
        "- When the user did NOT ask about 通知:\n"
        "  → Skip 通知 articles and use other labels.\n"
        "- Never recommend 通知 articles as casual reading options.\n"
        "\n"
        "SNOOZE HANDLING (IMPORTANT):\n"
        "- Snoozed articles that match the topic MUST still be shown as options.\n"
        "- When recommending a snoozed article, note it in the reason: '（已延后至X，可取消）'\n"
        "- Prioritize non-snoozed articles first, then snoozed ones as secondary choices.\n"
        "- If no non-snoozed articles match, present the snoozed matches.\n"
        "- Never say '没有找到' when there ARE matching articles (even if snoozed).\n"
        "- After showing snoozed matches, you may add 1-2 alternatives from other topics.\n"
        "\n"
        "FOLLOW-UP TOPIC REQUESTS:\n"
        "- If the user says '我想要X类的' / '有X方面的吗' / '换成X', acknowledge the filter.\n"
        "- If there are matching articles (including snoozed), always show them.\n"
        "- Only say '没有找到X类的文章' if the database truly has none.\n"
        "\n"
        "IMPORTANT OUTPUT FORMAT — you MUST use this exact format for each option:\n"
        "[OPTION:article_id] 【时长 · 标签】**标题** | 一句推荐理由\n"
        "\n"
        "The 【时长 · 标签】prefix lets users instantly judge whether to read. Examples:\n"
        "  【12分钟 · 时政】→ short time, politics\n"
        "  【3.5小时 · 专业前沿】→ long video, professional content\n"
        "  【2分钟 · 攻略】→ quick tip, practical\n"
        "\n"
        "Example output:\n"
        "---\n"
        "筛选到3篇时政相关：\n\n"
        "[OPTION:15] 【12分钟 · 时政】**两会政策解读** | 深入浅出，适合午休阅读\n"
        "[OPTION:22] 【8分钟 · 时政】**中美关系最新动态** | 快速了解关税谈判要点（已延后，可取消）\n"
        "[OPTION:30] 【15分钟 · 专业前沿】**AI Agent应用实践** | 技术干货，作为备选\n"
        "按需阅读，随时可以问我。\n"
        "---\n"
        "\n"
        "Rules:\n"
        "- Each option MUST start with [OPTION:id] 【时长 · 标签】format\n"
        "- Time comes directly from the article's read_time_display field\n"
        "- Label comes from the article's label field\n"
        "- End with ONE short line: briefly affirm what was found. Do NOT make the user choose\n"
        "  or ask rhetorical questions. Keep it direct, e.g. '按需阅读，随时可以问我。'\n"
        "- NEVER add up reading times or make the user do math.\n"
        "  Wrong: '这两篇共6分钟' / '搭配这篇刚好4分钟' / '合计10分钟'\n"
        "  Right: '时间短可以先读这篇' / '想深入了解的话推荐这篇'\n"
        "- If the question is NOT asking for recommendations or filtering, answer naturally without [OPTION:] format."
    )

    answer = _call_llm_query(api_config, answer_prompt, articles_context, max_tokens=600)
    if not answer:
        answer = f'找到 {len(rows)} 篇与「{question}」相关的文章。'

    # Track which articles were recommended to avoid repetition
    rec_ids = re.findall(r'\[OPTION:(\d+)\]', answer)
    for rid in rec_ids:
        _recommended_in_session.add(int(rid))

    return {
        'answer': answer,
        'articles': rows[:10],
        'query_type': 'llm',
        'total_matches': len(rows),
    }


def _keyword_query(question, stats):
    """Keyword-based search with scoring-based ranking. No LLM required."""
    intent = _classify_query_intent(question)

    # Recommendation / broad query: use scored retrieval
    if intent == 'recommendation':
        rows = database.get_recommendable_articles(limit=10)
        if not rows:
            return {
                'answer': f'你的知识库中有 {stats["total"]} 篇文章，但暂时没有未读文章。',
                'articles': [],
                'query_type': 'keyword_match',
                'total_matches': 0,
            }
        titles = [f'《{r["title"][:20]}》' for r in rows[:5]]
        return {
            'answer': f'你的知识库中有 {stats["total"]} 篇文章，其中 {stats["unread"]} 篇待读。以下是为你推荐的未读文章：\n\n' +
                      '\n'.join(f'• {t}' for t in titles) +
                      '\n\n配置 AI 后可获得更精准的智能推荐和推荐理由。',
            'articles': rows,
            'query_type': 'keyword_match',
            'total_matches': len(rows),
        }

    # Stats intent
    if intent == 'stats':
        label_list = ', '.join(f'{k}({v})' for k, v in stats['by_label'].items())
        return {
            'answer': f'知识库共 {stats["total"]} 篇，未读 {stats["unread"]} 篇，已读 {stats["read"]} 篇。\n分类分布：{label_list}。',
            'articles': [],
            'query_type': 'keyword_match',
            'total_matches': 0,
        }

    # Search: extract keywords and filter
    stop_words = {'我', '的', '有', '了', '是', '在', '吗', '呢', '吧', '啊',
                  '哪些', '什么', '怎么', '如何', '为什么', '帮我', '推荐',
                  '一篇', '文章', '应该', '可以', '有没有', '关于', '多少',
                  '还有', '未读', '已读', '所有', '全部', '总结', '几个',
                  '想要', '想要一', '一类的', '方面的', '相关的', '类的'}
    cn_keywords = re.findall(r'[一-鿿]{2,}', question)
    en_keywords = re.findall(r'[a-zA-Z]{2,}', question)
    keywords = cn_keywords + [kw.upper() for kw in en_keywords]
    keywords = [kw for kw in keywords if kw.lower() not in stop_words]

    known_labels = set(stats.get('by_label', {}).keys()) | {'通知', '文艺', '专业前沿', '时政', '娱乐', '攻略'}
    topic_labels = [kw for kw in keywords if kw in known_labels]
    if topic_labels:
        keywords = topic_labels

    if not keywords:
        rows = database.get_recommendable_articles(limit=10)
        titles = [f'《{r["title"][:20]}》' for r in rows[:5]]
        return {
            'answer': f'不太确定你想找什么类型。知识库共有 {stats["total"]} 篇，未读 {stats["unread"]} 篇。你可以试试问「推荐一篇时政类文章」或「有哪些攻略」。',
            'articles': rows,
            'query_type': 'keyword_match',
            'total_matches': len(rows),
        }

    # Build LIKE query then score
    conditions = []
    params = []
    for kw in keywords[:5]:
        like = f'%{kw}%'
        conditions.append(
            '(title LIKE ? OR full_content LIKE ? OR label LIKE ? '
            'OR id IN (SELECT article_id FROM article_user_labels WHERE label_name LIKE ?))'
        )
        params.extend([like, like, like, like])

    where = ' OR '.join(conditions)
    sql = f'SELECT * FROM articles WHERE ({where}) LIMIT 50'
    rows = database.execute_raw(sql, params)

    # Score results
    if rows:
        rows = database.compute_article_scores(list(rows))

    if not rows:
        rows = database.get_recommendable_articles(limit=10)
        if rows:
            titles = [f'《{r["title"][:20]}》' for r in rows[:5]]
            return {
                'answer': f'没有精确匹配「{question}」的文章，但你有 {stats["unread"]} 篇待读文章：\n' +
                          '\n'.join(f'• {t}' for t in titles),
                'articles': rows,
                'query_type': 'keyword_match',
                'total_matches': len(rows),
            }
        return {
            'answer': f'没有找到与「{question}」相关的文章。知识库共有 {stats["total"]} 篇文章。',
            'articles': [],
            'query_type': 'keyword_match',
            'total_matches': 0,
        }

    titles = [f'《{r["title"][:20]}》' for r in rows[:5]]
    return {
        'answer': f'找到 {len(rows)} 篇相关文章：\n' + '\n'.join(f'• {t}' for t in titles),
        'articles': rows[:10],
        'query_type': 'keyword_match',
        'total_matches': len(rows),
    }


@app.route('/api/query', methods=['POST'])
def query_database():
    """Natural language query over the article database."""
    data = request.get_json()
    if not data or 'question' not in data:
        return jsonify({'success': False, 'error': '请输入问题'}), 400

    question = data['question'].strip()
    if not question:
        return jsonify({'success': False, 'error': '请输入问题'}), 400

    api_config = data.get('api_config', {})
    history = data.get('history', [])  # multi-turn conversation context

    # Fall back to server-stored config if frontend didn't provide one
    if not api_config.get('api_key'):
        api_config = database.get_config('llm_config') or {}

    stats = database.get_stats()

    if stats['total'] == 0:
        return jsonify({
            'success': True,
            'answer': '你的知识库还是空的哦~ 先粘贴一些文章链接，我帮你分析归档后，就可以问我问题了！',
            'articles': [],
            'stats': stats,
        })

    # Try LLM-based query if configured
    if api_config.get('api_key'):
        result = _llm_query(question, api_config, stats, history=history)
        if result:
            result['success'] = True
            result['stats'] = stats
            return jsonify(result)

    # Fall back to keyword matching
    result = _keyword_query(question, stats)
    result['success'] = True
    result['stats'] = stats
    return jsonify(result)


@app.route('/api/feedback', methods=['POST'])
def record_feedback():
    """Record user feedback on article recommendations.
    Accepts: {article_id, event_type, payload?}
    event_type: 'opened' | 'read_done' | 'dismiss' | 'snooze'
    """
    data = request.get_json()
    if not data or 'article_id' not in data or 'event_type' not in data:
        return jsonify({'success': False, 'error': '请提供 article_id 和 event_type'}), 400

    article_id = data['article_id']
    event_type = data['event_type']
    payload = data.get('payload', None)

    if event_type not in ('opened', 'read_done', 'dismiss', 'snooze'):
        return jsonify({'success': False, 'error': f'无效的 event_type: {event_type}'}), 400

    # Verify article exists before recording event (FK constraint)
    article = database.get_article(article_id)
    if not article:
        return jsonify({'success': False, 'error': '文章不存在'}), 404

    # For snooze, payload must include snooze_until
    if event_type == 'snooze':
        if not payload or 'snooze_until' not in payload:
            return jsonify({'success': False, 'error': '延后操作需要提供 snooze_until'}), 400

    database.record_event(article_id, event_type, payload)
    new_score = database.get_article_score(article_id)

    return jsonify({
        'success': True,
        'event': event_type,
        'article_id': article_id,
        'new_score': new_score,
    })


@app.route('/api/presets', methods=['GET'])
def get_presets():
    """Return preset labels from label-prompt.md."""
    presets = load_preset_labels()
    # Strip heavy prompt content for list display
    slim = []
    for p in presets:
        slim.append({
            'id': p['id'],
            'name': p['name'],
            'description': p['description'],
            'keywords': p['keywords'],
            'priority': p['priority'],
            'is_preset': True,
        })
    return jsonify({'success': True, 'labels': slim})


@app.route('/api/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({'status': 'ok', 'time': time.time()})


if __name__ == '__main__':
    import sys
    port = int(os.environ.get('PORT', 8080))
    debug = '--debug' in sys.argv
    print('=' * 55)
    print('  「稍后看」服务已启动')
    print(f'  访问: http://localhost:{port}')
    print('')
    print('  💡 点击页面顶部「未接AI」按钮配置大模型')
    print('     支持 DeepSeek / 腾讯混元 / 自定义接口')
    print('     API Key 仅保存在浏览器本地')
    print('=' * 55)
    app.run(host='0.0.0.0', port=port, debug=debug)
