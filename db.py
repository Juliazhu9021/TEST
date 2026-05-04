"""
稍后看 - SQLite Database Layer
Handles all persistent storage for articles, labels, and user configuration.
"""
import sqlite3
import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'laterread.db')


def get_db():
    """Get database connection with dict-like row access."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables and indexes if they don't exist. Idempotent."""
    conn = get_db()
    try:
        # Migration: add content_type column for existing databases
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN content_type TEXT DEFAULT 'article'")
        except sqlite3.OperationalError:
            pass  # column already exists

        # Migration: add snooze_until, dismiss_count, and deadline_at
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN snooze_until TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN dismiss_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN deadline_at TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN ddl_pending INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN extracted_deadline TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                url             TEXT NOT NULL UNIQUE,
                title           TEXT NOT NULL DEFAULT '未命名文章',
                author          TEXT DEFAULT '',
                source          TEXT DEFAULT '',
                full_content    TEXT DEFAULT '',
                content_preview TEXT DEFAULT '',
                summary         TEXT DEFAULT '',
                summary_mode    TEXT DEFAULT 'extractive',
                label           TEXT DEFAULT '未分类',
                label_confidence INTEGER DEFAULT 0,
                label_scores    TEXT DEFAULT '{}',
                read_time_min   INTEGER DEFAULT 1,
                read_time_display TEXT DEFAULT '',
                level           TEXT DEFAULT '中阶',
                content_type    TEXT DEFAULT 'article',
                is_read         INTEGER DEFAULT 0,
                read_at         TEXT DEFAULT NULL,
                label_confirmed INTEGER DEFAULT 0,
                snooze_until    TEXT DEFAULT NULL,
                dismiss_count   INTEGER DEFAULT 0,
                deadline_at     TEXT DEFAULT NULL,
                ddl_pending     INTEGER DEFAULT 0,
                extracted_deadline TEXT DEFAULT NULL,
                created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS article_user_labels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id  INTEGER NOT NULL,
                label_name  TEXT NOT NULL,
                FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
                UNIQUE(article_id, label_name)
            );

            CREATE TABLE IF NOT EXISTS user_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id  INTEGER NOT NULL,
                event_type  TEXT NOT NULL,
                payload     TEXT DEFAULT '{}',
                created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_events_article
                ON user_events(article_id);
            CREATE INDEX IF NOT EXISTS idx_user_events_type
                ON user_events(event_type);

            CREATE INDEX IF NOT EXISTS idx_article_labels_article
                ON article_user_labels(article_id);
            CREATE INDEX IF NOT EXISTS idx_article_labels_name
                ON article_user_labels(label_name);
            CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url);
            CREATE INDEX IF NOT EXISTS idx_articles_label ON articles(label);
            CREATE INDEX IF NOT EXISTS idx_articles_is_read ON articles(is_read);
            CREATE INDEX IF NOT EXISTS idx_articles_created ON articles(created_at);

            CREATE TABLE IF NOT EXISTS user_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
        """)
        conn.commit()
        logger.info(f'Database initialized at {DB_PATH}')
    finally:
        conn.close()


# ── Row → Dict helper ──

def _row_to_dict(row):
    """Convert sqlite3.Row to plain dict, deserializing JSON fields."""
    if row is None:
        return None
    d = dict(row)
    # Deserialize JSON fields
    if d.get('label_scores') and isinstance(d['label_scores'], str):
        try:
            d['label_scores'] = json.loads(d['label_scores'])
        except json.JSONDecodeError:
            d['label_scores'] = {}
    # Sanitize label to catch any pre-fix stored XSS
    if 'label' in d and d['label'] and ('<' in d['label'] or '>' in d['label']):
        d['label'] = '未分类'
    return d


def _sanitize_labels(labels):
    """Filter out label names containing HTML characters (defense in depth)."""
    if not labels:
        return labels
    return [l for l in labels if isinstance(l, str) and '<' not in l and '>' not in l]


def _format_timestamp():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ── Article CRUD ──

def insert_article(article):
    """Insert a new article. Returns the new article ID."""
    conn = get_db()
    try:
        label_scores = article.get('label_scores', {})
        if isinstance(label_scores, dict):
            label_scores = json.dumps(label_scores, ensure_ascii=False)
        else:
            label_scores = '{}'

        cursor = conn.execute(
            """INSERT INTO articles (url, title, author, source, full_content,
               content_preview, summary, summary_mode, label, label_confidence,
               label_scores, read_time_min, read_time_display, level, content_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                article.get('url', ''),
                article.get('title', '未命名文章'),
                article.get('author', ''),
                article.get('source', ''),
                article.get('full_content', article.get('content', '')),
                article.get('content_preview', ''),
                article.get('summary', ''),
                article.get('summary_mode', 'extractive'),
                article.get('label', '未分类'),
                article.get('label_confidence', 0),
                label_scores,
                article.get('read_time_min', 1),
                article.get('read_time_display', ''),
                article.get('level', '中阶'),
                article.get('content_type', 'article'),
            )
        )
        conn.commit()
        article_id = cursor.lastrowid
        logger.info(f'Article #{article_id} inserted: {article.get("title", "")[:40]}')
        return article_id
    finally:
        conn.close()


def get_article(article_id):
    """Get single article by ID with user labels joined."""
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT * FROM articles WHERE id = ?', (article_id,)
        ).fetchone()
        if not row:
            return None
        article = _row_to_dict(row)
        # Attach user labels
        labels = conn.execute(
            'SELECT label_name FROM article_user_labels WHERE article_id = ?',
            (article_id,)
        ).fetchall()
        article['user_labels'] = _sanitize_labels([l['label_name'] for l in labels])
        return article
    finally:
        conn.close()


def get_articles(filter='all', search='', limit=50, offset=0):
    """List articles with optional filtering and search. Returns (articles, total)."""
    conn = get_db()
    try:
        where_clauses = []
        params = []

        if filter == 'unread':
            where_clauses.append('a.is_read = 0')
        elif filter == 'read':
            where_clauses.append('a.is_read = 1')
        elif filter and filter not in ('all', 'unread', 'read'):
            # Filter by label name
            where_clauses.append(
                '(a.label = ? OR a.id IN '
                '(SELECT article_id FROM article_user_labels WHERE label_name = ?))'
            )
            params.extend([filter, filter])

        if search:
            where_clauses.append(
                '(a.title LIKE ? OR a.full_content LIKE ? OR a.label LIKE ?)'
            )
            like = f'%{search}%'
            params.extend([like, like, like])

        where_sql = ('WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''

        count_row = conn.execute(
            f'SELECT COUNT(*) as cnt FROM articles a {where_sql}', params
        ).fetchone()
        total = count_row['cnt'] if count_row else 0

        rows = conn.execute(
            f'''SELECT a.* FROM articles a {where_sql}
                ORDER BY a.created_at DESC LIMIT ? OFFSET ?''',
            params + [limit, offset]
        ).fetchall()

        articles = []
        for row in rows:
            article = _row_to_dict(row)
            # Exclude full_content from list responses
            article.pop('full_content', None)
            # Attach user labels
            labels = conn.execute(
                'SELECT label_name FROM article_user_labels WHERE article_id = ?',
                (article['id'],)
            ).fetchall()
            article['user_labels'] = _sanitize_labels([l['label_name'] for l in labels])
            articles.append(article)

        return articles, total
    finally:
        conn.close()


def update_article(article_id, updates):
    """Update article fields. Handles user_labels separately."""
    conn = get_db()
    try:
        ts = _format_timestamp()

        # Handle user_labels
        if 'user_labels' in updates:
            label_names = updates.pop('user_labels')
            if label_names and isinstance(label_names, list):
                set_article_labels_internal(conn, article_id, label_names)

        if updates:
            # Map frontend camelCase to DB snake_case — only allow known column names
            field_map = {
                'isRead': 'is_read',
                'readAt': 'read_at',
                'labelConfirmed': 'label_confirmed',
                'snoozeUntil': 'snooze_until',
                'deadlineAt': 'deadline_at',
                'ddlPending': 'ddl_pending',
                'extractedDeadline': 'extracted_deadline',
            }
            allowed_columns = {'is_read', 'read_at', 'label_confirmed', 'label',
                               'snooze_until', 'deadline_at', 'dismiss_count',
                               'ddl_pending', 'extracted_deadline'}
            db_updates = {}
            for k, v in updates.items():
                db_key = field_map.get(k, k)
                if db_key not in allowed_columns:
                    logger.warning(f'Rejected update to unknown column: {db_key}')
                    continue
                db_updates[db_key] = v

            if db_updates:
                set_clauses = ', '.join(f'{k} = ?' for k in db_updates)
                set_clauses += ', updated_at = ?'
                values = list(db_updates.values()) + [ts]
                conn.execute(
                    f'UPDATE articles SET {set_clauses} WHERE id = ?',
                    values + [article_id]
                )

        conn.commit()
        return get_article(article_id)
    finally:
        conn.close()


def delete_article(article_id):
    """Delete an article. Cascade deletes user_labels. Returns True if deleted, False if not found."""
    conn = get_db()
    try:
        cur = conn.execute('DELETE FROM articles WHERE id = ?', (article_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_articles_batch(ids):
    """Delete multiple articles at once. Returns count of deleted rows."""
    if not ids:
        return 0
    conn = get_db()
    try:
        placeholders = ','.join(['?' for _ in ids])
        cursor = conn.execute(f'DELETE FROM articles WHERE id IN ({placeholders})', ids)
        deleted = cursor.rowcount if cursor.rowcount >= 0 else len(ids)
        conn.commit()
        return deleted
    finally:
        conn.close()


def check_duplicate_url(url):
    """Return existing article if URL already in DB, else None."""
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT * FROM articles WHERE url = ?', (url,)
        ).fetchone()
        if not row:
            return None
        article = _row_to_dict(row)
        article.pop('full_content', None)
        labels = conn.execute(
            'SELECT label_name FROM article_user_labels WHERE article_id = ?',
            (article['id'],)
        ).fetchall()
        article['user_labels'] = _sanitize_labels([l['label_name'] for l in labels])
        return article
    finally:
        conn.close()


# ── User Labels ──

def set_article_labels_internal(conn, article_id, label_names):
    """Replace all user labels for an article (uses existing connection)."""
    conn.execute(
        'DELETE FROM article_user_labels WHERE article_id = ?', (article_id,)
    )
    for name in label_names:
        if name and name != '未分类':
            conn.execute(
                'INSERT OR IGNORE INTO article_user_labels (article_id, label_name) VALUES (?, ?)',
                (article_id, name)
            )


def set_article_labels(article_id, label_names):
    """Replace all user labels for an article."""
    conn = get_db()
    try:
        set_article_labels_internal(conn, article_id, label_names)
        conn.commit()
    finally:
        conn.close()


# ── Config ──

def get_config(key):
    """Get a config value by key, JSON-decoded."""
    conn = get_db()
    try:
        row = conn.execute(
            'SELECT value FROM user_config WHERE key = ?', (key,)
        ).fetchone()
        if not row:
            return None
        return json.loads(row['value'])
    except (json.JSONDecodeError, KeyError):
        return None
    finally:
        conn.close()


def set_config(key, value):
    """Upsert a config value as JSON."""
    conn = get_db()
    try:
        ts = _format_timestamp()
        conn.execute(
            '''INSERT INTO user_config (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at''',
            (key, json.dumps(value, ensure_ascii=False), ts)
        )
        conn.commit()
    finally:
        conn.close()


# ── User Events (Feedback) ──

def record_event(article_id, event_type, payload=None):
    """Record a user feedback event and update article state accordingly."""
    conn = get_db()
    try:
        ts = _format_timestamp()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)

        conn.execute(
            """INSERT INTO user_events (article_id, event_type, payload, created_at)
               VALUES (?, ?, ?, ?)""",
            (article_id, event_type, payload_json, ts)
        )

        # Update article state based on event type
        if event_type == 'snooze' and payload and payload.get('snooze_until'):
            conn.execute(
                'UPDATE articles SET snooze_until = ?, updated_at = ? WHERE id = ?',
                (payload['snooze_until'], ts, article_id)
            )
        elif event_type == 'dismiss':
            conn.execute(
                'UPDATE articles SET dismiss_count = dismiss_count + 1, updated_at = ? WHERE id = ?',
                (ts, article_id)
            )

        conn.commit()
        return True
    finally:
        conn.close()


def get_article_events(article_id):
    """Get all feedback events for an article."""
    conn = get_db()
    try:
        rows = conn.execute(
            'SELECT * FROM user_events WHERE article_id = ? ORDER BY created_at DESC',
            (article_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _get_article_score_internal(conn, article_id, user_profile=None):
    """Compute unified recommendation score using existing connection.

    Signals:
    - Base 30
    - Unread +20 | Already read -50
    - Label confidence >= 60: +8
    - Per-article dismiss: -20 each
    - Opened but not finished (interrupted interest): +20
    - Same-label dismiss penalty: -8 per dismiss of other articles with same label
    - Label-level dismiss penalty: -12 per dismiss of any article with same label
    - Time-sensitive (通知): +5~20 based on recency
    - Fresh content (<=3 days): +5
    - Video format: +5
    - Active snooze: returns 0
    """
    if user_profile is None:
        user_profile = _build_user_profile(conn)

    row = conn.execute('SELECT * FROM articles WHERE id = ?', (article_id,)).fetchone()
    if not row:
        return 0
    a = _row_to_dict(row)

    # Active snooze → excluded entirely
    if a.get('snooze_until'):
        from datetime import datetime as _dt
        try:
            until = _dt.strptime(a['snooze_until'], '%Y-%m-%d %H:%M:%S')
            if until > _dt.now():
                return 0
        except (ValueError, TypeError):
            pass

    score = 30

    # ── Read status ──
    if a.get('is_read'):
        score -= 50
    else:
        score += 20

    # ── Label confidence ──
    if a.get('label_confidence', 0) >= 60:
        score += 8

    # ── Per-article dismiss ──
    dismiss_count = a.get('dismiss_count', 0)
    score -= dismiss_count * 20

    # ── Opened but not finished (interrupted interest) ──
    opened = conn.execute(
        "SELECT COUNT(*) as cnt FROM user_events WHERE article_id = ? AND event_type = 'opened'",
        (article_id,)
    ).fetchone()
    done = conn.execute(
        "SELECT COUNT(*) as cnt FROM user_events WHERE article_id = ? AND event_type = 'read_done'",
        (article_id,)
    ).fetchone()
    if opened['cnt'] > 0 and done['cnt'] == 0:
        score += 20

    # ── Same-label dismiss penalty ──
    article_label = (a.get('label', '') or '').strip()
    if article_label and article_label != '未分类':
        same_dismiss = conn.execute(
            """SELECT COUNT(*) as cnt FROM user_events ue
               JOIN articles ar ON ue.article_id = ar.id
               WHERE ue.event_type = 'dismiss' AND ar.label = ? AND ue.article_id != ?""",
            (article_label, article_id)
        ).fetchone()
        score -= same_dismiss['cnt'] * 8

    # ── Label-level global dismiss penalty ──
    dismissed_labels = user_profile.get('dismissed_labels', {})
    if article_label and article_label in dismissed_labels:
        score -= dismissed_labels[article_label] * 12

    # ── Label preference match (from weighted profile built on user history) ──
    top_labels = user_profile.get('top_labels', [])
    article_label = (a.get('label', '') or '').strip()
    if article_label and article_label != '未分类':
        for rank, lb in enumerate(top_labels):
            if lb['label'] == article_label:
                if rank == 0:
                    score += 25   # user's #1 favorite topic
                elif rank == 1:
                    score += 14   # second favorite
                else:
                    score += 7    # in top-5 interest range
                break

    # ── Label engagement bonus: more reads of this label = stronger preference ──
    if article_label and article_label != '未分类':
        label_read = conn.execute(
            "SELECT COUNT(*) as cnt FROM articles WHERE label = ? AND is_read = 1", (article_label,)
        ).fetchone()['cnt']
        if label_read >= 2:
            score += min(label_read * 3, 18)  # +3 per completed read, cap +18

    # ── Time sensitivity: 通知 recency boost ──
    if article_label == '通知':
        try:
            from datetime import datetime as _dt
            created = _dt.strptime(a['created_at'], '%Y-%m-%d %H:%M:%S')
            days = (_dt.now() - created).days
            if days <= 3:
                score += 20
            elif days <= 7:
                score += 10
            elif days <= 14:
                score += 5
        except (ValueError, TypeError):
            pass

    # ── Content freshness ──
    days_since = None
    try:
        from datetime import datetime as _dt
        created = _dt.strptime(a['created_at'], '%Y-%m-%d %H:%M:%S')
        days_since = (_dt.now() - created).days
        if days_since <= 2:
            score += 5
    except (ValueError, TypeError):
        pass

    # ── Collection age (anti-dust): older unread → boost ──
    if not a.get('is_read') and days_since is not None:
        if days_since >= 14:
            score += 18
        elif days_since >= 7:
            score += 10
        elif days_since >= 3:
            score += 5

    # ── Video engagement bonus ──
    if a.get('content_type') == 'video':
        score += 5

    return max(score, 0)


def _get_personalized_score_internal(conn, article_id, user_profile=None):
    """Compute personalized recommendation score (no time-based factors).

    Signals:
    - Base 30
    - Unread +20 | Already read -50
    - Label confidence >= 60: +8
    - Per-article dismiss: -20 each
    - Opened but not finished: +20
    - Same-label dismiss: -8 per
    - Label-level global dismiss penalty: -12 per dismiss of same label
    - Label preference match (top-1): +20 | second: +10 | top-5: +5
    - Reading-time match (within user's preferred range): +5
    - Video: +5
    - Active snooze: returns 0
    """
    if user_profile is None:
        user_profile = _build_user_profile(conn)

    row = conn.execute('SELECT * FROM articles WHERE id = ?', (article_id,)).fetchone()
    if not row:
        return 0
    a = _row_to_dict(row)

    # Active snooze → excluded
    if a.get('snooze_until'):
        from datetime import datetime as _dt
        try:
            until = _dt.strptime(a['snooze_until'], '%Y-%m-%d %H:%M:%S')
            if until > _dt.now():
                return 0
        except (ValueError, TypeError):
            pass

    # Days since collection (used by collection-age signal)
    days_since = None
    try:
        from datetime import datetime as _dt
        created = _dt.strptime(a['created_at'], '%Y-%m-%d %H:%M:%S')
        days_since = (_dt.now() - created).days
    except (ValueError, TypeError):
        pass

    score = 30

    # ── Read status ──
    if a.get('is_read'):
        score -= 50
    else:
        score += 20

    # ── Label confidence ──
    if a.get('label_confidence', 0) >= 60:
        score += 8

    # ── Per-article dismiss ──
    dismiss_count = a.get('dismiss_count', 0)
    score -= dismiss_count * 20

    # ── Opened but not finished ──
    opened = conn.execute(
        "SELECT COUNT(*) as cnt FROM user_events WHERE article_id = ? AND event_type = 'opened'",
        (article_id,)
    ).fetchone()
    done = conn.execute(
        "SELECT COUNT(*) as cnt FROM user_events WHERE article_id = ? AND event_type = 'read_done'",
        (article_id,)
    ).fetchone()
    if opened['cnt'] > 0 and done['cnt'] == 0:
        score += 20

    # ── Same-label dismiss penalty ──
    article_label = (a.get('label', '') or '').strip()
    if article_label and article_label != '未分类':
        same_dismiss = conn.execute(
            """SELECT COUNT(*) as cnt FROM user_events ue
               JOIN articles ar ON ue.article_id = ar.id
               WHERE ue.event_type = 'dismiss' AND ar.label = ? AND ue.article_id != ?""",
            (article_label, article_id)
        ).fetchone()
        score -= same_dismiss['cnt'] * 8

    # ── Label-level global dismiss penalty ──
    dismissed_labels = user_profile.get('dismissed_labels', {})
    if article_label and article_label in dismissed_labels:
        score -= dismissed_labels[article_label] * 12

    # ── Label engagement bonus: more reads of this label = stronger preference ──
    if article_label and article_label != '未分类':
        label_read = conn.execute(
            "SELECT COUNT(*) as cnt FROM articles WHERE label = ? AND is_read = 1", (article_label,)
        ).fetchone()['cnt']
        if label_read >= 2:
            score += min(label_read * 3, 18)  # +3 per completed read, cap +18

    # ── Label preference match (from weighted profile) ──
    top_labels = user_profile.get('top_labels', [])
    for rank, lb in enumerate(top_labels):
        if lb['label'] == article_label:
            if rank == 0:
                score += 20
            elif rank == 1:
                score += 10
            else:
                score += 5
            break

    # ── Reading-time preference ──
    pref_time = user_profile.get('preferred_time_min')
    if pref_time and a.get('read_time_min'):
        rt = a['read_time_min']
        if pref_time * 0.5 <= rt <= pref_time * 1.5:
            score += 5

    # ── Collection age (anti-dust) ──
    if not a.get('is_read') and days_since is not None:
        if days_since >= 14:
            score += 18
        elif days_since >= 7:
            score += 10
        elif days_since >= 3:
            score += 5

    # ── Video bonus ──
    if a.get('content_type') == 'video':
        score += 5

    return max(score, 0)


def _get_article_reasons(conn, article, user_profile=None):
    """Return top 2 human-readable reasons why this article scored well.

    Returns list of {signal, text, priority}, sorted by priority ASC.
    """
    if user_profile is None:
        user_profile = _build_user_profile(conn)

    reasons = []
    a = article
    aid = a['id']

    days_since = None
    try:
        from datetime import datetime as _dt
        created = _dt.strptime(a['created_at'], '%Y-%m-%d %H:%M:%S')
        days_since = (_dt.now() - created).days
    except (ValueError, TypeError):
        pass

    article_label = (a.get('label', '') or '').strip()
    is_unread = not a.get('is_read')
    top_labels = user_profile.get('top_labels', [])
    dismissed_labels = user_profile.get('dismissed_labels', {})

    # P1: Label preference Top1 (+20)
    if top_labels and article_label and top_labels[0]['label'] == article_label:
        reasons.append({'signal': 'label_top1', 'text': '你最爱读这类', 'priority': 1})

    # P2: 通知 <=3d (+20)
    if article_label == '通知' and days_since is not None and days_since <= 3:
        reasons.append({'signal': 'notif_3d', 'text': '刚发布，有时效要求', 'priority': 2})

    # P3: 收藏 >=14d (+18)
    if is_unread and days_since is not None and days_since >= 14:
        reasons.append({'signal': 'old_collection', 'text': f'已收藏 {days_since} 天，该看看了', 'priority': 3})

    # P4: Opened unfinished (+20)
    opened = conn.execute(
        "SELECT COUNT(*) as cnt FROM user_events WHERE article_id=? AND event_type='opened'", (aid,)
    ).fetchone()
    done = conn.execute(
        "SELECT COUNT(*) as cnt FROM user_events WHERE article_id=? AND event_type='read_done'", (aid,)
    ).fetchone()
    if opened['cnt'] > 0 and done['cnt'] == 0:
        reasons.append({'signal': 'unfinished', 'text': '上次没看完', 'priority': 4})

    # P5: Label preference Top2 (+10)
    if len(top_labels) > 1 and article_label and top_labels[1]['label'] == article_label:
        reasons.append({'signal': 'label_top2', 'text': '符合你近期的阅读偏好', 'priority': 5})

    # P6: Label confidence high (+8)
    if a.get('label_confidence', 0) >= 60 and article_label and article_label != '未分类':
        reasons.append({'signal': 'confidence', 'text': f'AI 识别为「{article_label}」', 'priority': 6})

    # P7: 收藏 7-13d (+10)
    if is_unread and days_since is not None and 7 <= days_since < 14:
        reasons.append({'signal': 'collection_7d', 'text': f'已放 {days_since} 天，抽空读', 'priority': 7})

    # P8: 通知 <=7d (+10)
    if article_label == '通知' and days_since is not None and 3 < days_since <= 7:
        reasons.append({'signal': 'notif_7d', 'text': '近期通知', 'priority': 8})

    # P9: User manually labeled
    labels_row = conn.execute(
        "SELECT label_name FROM article_user_labels WHERE article_id=?", (aid,)
    ).fetchall()
    if labels_row:
        reasons.append({'signal': 'manual_label', 'text': '你手动标记过', 'priority': 9})

    # P10: Label top 3-5 (+5)
    for lb in top_labels[2:5]:
        if lb['label'] == article_label:
            reasons.append({'signal': 'label_top5', 'text': '和你兴趣范围相关', 'priority': 10})
            break

    # P11: Reading time match (+5)
    pref = user_profile.get('preferred_time_min')
    rt = a.get('read_time_min')
    if pref and rt and pref * 0.5 <= rt <= pref * 1.5:
        reasons.append({'signal': 'time_match', 'text': f'约 {rt} 分钟，符合习惯', 'priority': 11})

    # P12: 收藏 3-6d (+5)
    if is_unread and days_since is not None and 3 <= days_since < 7:
        reasons.append({'signal': 'collection_3d', 'text': f'{days_since} 天前加入的', 'priority': 12})

    # P13: Fresh <=2d (+5)
    if days_since is not None and days_since <= 2:
        reasons.append({'signal': 'fresh', 'text': '刚加入不久', 'priority': 13})

    # P14: Video (+5)
    if a.get('content_type') == 'video':
        reasons.append({'signal': 'video', 'text': '视频内容', 'priority': 14})

    # P15: 通知 <=14d (+5)
    if article_label == '通知' and days_since is not None and 7 < days_since <= 14:
        reasons.append({'signal': 'notif_14d', 'text': '来自通知中心', 'priority': 15})

    reasons.sort(key=lambda r: r['priority'])
    return reasons[:2]


def _build_user_profile(conn):
    """Build a lightweight reading-preference profile from user history.

    Uses event-type weighting and exponential time decay. Excludes 通知
    from preference building — notifications are obligations, not interests.

    Returns dict with:
    - top_labels: [{label, score}] sorted by weighted score DESC (top 5)
    - preferred_time_min: mode of reading times, or None
    - label_scores: {label: weighted_score}
    - dismissed_labels: {label: total_dismiss_count}
    """
    import math
    from datetime import datetime as _dt

    profile = {
        'top_labels': [],
        'preferred_time_min': None,
        'label_scores': {},
        'dismissed_labels': {},
    }

    # Event type base weights
    EVENT_WEIGHTS = {
        'read_done': 3.0,
        'opened': 1.0,
        'snooze': 0.5,
        'dismiss': -2.0,
    }

    # Half-life for exponential time decay (14 days)
    HALF_LIFE = 14.0
    DECAY_LAMBDA = 1.0 / HALF_LIFE

    now = _dt.now()

    rows = conn.execute(
        """SELECT ue.event_type, ue.created_at, ar.label, ar.read_time_min
           FROM user_events ue
           JOIN articles ar ON ue.article_id = ar.id
           WHERE ar.label IS NOT NULL AND ar.label != '未分类'
             AND ar.label != '通知'""",
    ).fetchall()

    label_scores = {}
    dismissed_labels = {}
    time_buckets = {}  # {read_time_min: weighted_count}

    for r in rows:
        label = r['label']
        if '<' in label or '>' in label:
            continue
        event_type = r['event_type']
        base_weight = EVENT_WEIGHTS.get(event_type, 0)

        # Compute exponential time decay: weight * e^(-days/14)
        try:
            created = _dt.strptime(r['created_at'], '%Y-%m-%d %H:%M:%S')
            days = max((now - created).days, 0)
        except (ValueError, TypeError):
            days = 7

        decay = math.exp(-DECAY_LAMBDA * days)
        weighted = base_weight * decay

        if weighted > 0:
            label_scores[label] = label_scores.get(label, 0) + weighted
        elif event_type == 'dismiss':
            dismissed_labels[label] = dismissed_labels.get(label, 0) + 1

        # Track reading time preference (read_done + opened, weighted by recency)
        if event_type in ('read_done', 'opened') and r['read_time_min'] is not None:
            rt = r['read_time_min']
            time_buckets[rt] = time_buckets.get(rt, 0) + abs(weighted)

    # Sort labels by weighted score
    sorted_labels = sorted(label_scores.items(), key=lambda x: x[1], reverse=True)
    profile['top_labels'] = [{'label': l, 'score': round(s, 1)} for l, s in sorted_labels[:5]]
    profile['label_scores'] = dict(sorted_labels)
    profile['dismissed_labels'] = dismissed_labels

    # Preferred reading time: mode weighted by recency
    if time_buckets:
        profile['preferred_time_min'] = max(time_buckets, key=time_buckets.get)

    return profile


def get_article_score(article_id):
    """Public wrapper: compute unified recommendation score."""
    conn = get_db()
    try:
        return _get_article_score_internal(conn, article_id)
    finally:
        conn.close()


def _diversity_rerank(scored_articles, article_labels, lambda_diversity=0.3):
    """Re-rank scored articles to avoid clustering same-label articles consecutively.

    Greedy algorithm:
    1. Take the highest-scored article
    2. For remaining candidates: if label matches the previous selection,
       temporarily reduce score by lambda_diversity factor
    3. Re-sort and repeat

    Args:
        scored_articles: list of (article_id, score) sorted by score DESC
        article_labels: dict of {article_id: label}
        lambda_diversity: penalty factor for same-label (0.3 = reduce to 70%)

    Returns: reordered list of (article_id, diversity_adjusted_score, original_score)
    """
    if len(scored_articles) <= 1:
        return [(aid, s, s) for aid, s in scored_articles]

    remaining = list(scored_articles)  # (id, score)
    result = []
    last_label = None

    while remaining:
        penalized = []
        for aid, s in remaining:
            if article_labels.get(aid) == last_label:
                penalized.append((aid, s * (1.0 - lambda_diversity), s))
            else:
                penalized.append((aid, s, s))

        penalized.sort(key=lambda x: x[1], reverse=True)
        best = penalized[0]
        result.append((best[0], best[1], best[2]))
        last_label = article_labels.get(best[0])
        remaining = [(aid, orig) for aid, _, orig in penalized[1:]]

    return result


def get_recommendable_articles(limit=10, exclude_notification=True):
    """Get articles eligible for recommendation, sorted by unified score DESC.
    Fetches candidates then scores in Python for consistent single-source scoring.
    """
    conn = get_db()
    try:
        now = _format_timestamp()
        profile = _build_user_profile(conn)
        sql = """SELECT id FROM articles
                 WHERE (snooze_until IS NULL OR snooze_until <= ?)
                 AND is_read = 0"""
        params = [now]
        if exclude_notification:
            top_label_names = {lb['label'] for lb in profile.get('top_labels', [])}
            if '通知' not in top_label_names:
                sql += " AND label != '通知'"
        sql += " ORDER BY created_at DESC LIMIT 50"

        rows = conn.execute(sql, params).fetchall()
        scored = []
        for r in rows:
            s = _get_article_score_internal(conn, r['id'], user_profile=profile)
            if s > 0:
                scored.append((r['id'], s))
        scored.sort(key=lambda x: x[1], reverse=True)

        # Build label lookup for diversity rerank (use scored IDs only)
        scored_ids = [aid for aid, _ in scored]
        if not scored_ids:
            return []
        aid_to_label = {}
        placeholders = ','.join(['?' for _ in scored_ids])
        for r in conn.execute(
            f'SELECT id, label FROM articles WHERE id IN ({placeholders})',
            scored_ids,
        ).fetchall():
            aid_to_label[r['id']] = r['label']
        reranked = _diversity_rerank(scored, aid_to_label)

        result = []
        for article_id, div_score, orig_score in reranked[:limit]:
            article = _row_to_dict(
                conn.execute('SELECT * FROM articles WHERE id = ?', (article_id,)).fetchone()
            )
            if article:
                article['_score'] = orig_score
                article['_reasons'] = _get_article_reasons(conn, article, user_profile=profile)
                ulabels = conn.execute(
                    'SELECT label_name FROM article_user_labels WHERE article_id = ?', (article_id,)
                ).fetchall()
                article['user_labels'] = _sanitize_labels([l['label_name'] for l in ulabels])
                result.append(article)
        return result
    finally:
        conn.close()


def get_top_article():
    """Return the single highest-scored article (for push notifications)."""
    articles = get_recommendable_articles(limit=1)
    return articles[0] if articles else None


def get_personalized_top(limit=1, exclude_ids=None):
    """Return top article(s) scored by personalized preference (no time factors).
    Optionally exclude article IDs already seen in this session.
    """
    if exclude_ids is None:
        exclude_ids = []
    conn = get_db()
    try:
        now = _format_timestamp()
        profile = _build_user_profile(conn)
        exclude_set = set(exclude_ids)
        sql = """SELECT id FROM articles
                 WHERE (snooze_until IS NULL OR snooze_until <= ?)
                 AND is_read = 0
                 AND label != '通知'"""
        params = [now]
        sql += " ORDER BY created_at DESC LIMIT 50"
        rows = conn.execute(sql, params).fetchall()

        scored = []
        for r in rows:
            if r['id'] in exclude_set:
                continue
            s = _get_personalized_score_internal(conn, r['id'], user_profile=profile)
            if s > 0:
                scored.append((r['id'], s))
        scored.sort(key=lambda x: x[1], reverse=True)

        # Diversity rerank: avoid consecutive same-label articles
        scored_ids = [aid for aid, _ in scored]
        if not scored_ids:
            return []
        aid_to_label = {}
        placeholders = ','.join(['?' for _ in scored_ids])
        for r in conn.execute(
            f'SELECT id, label FROM articles WHERE id IN ({placeholders})',
            scored_ids,
        ).fetchall():
            aid_to_label[r['id']] = r['label']
        reranked = _diversity_rerank(scored, aid_to_label)

        result = []
        for article_id, div_score, orig_score in reranked[:limit]:
            article = _row_to_dict(
                conn.execute('SELECT * FROM articles WHERE id = ?', (article_id,)).fetchone()
            )
            if article:
                article['_score'] = orig_score
                article['_profile'] = profile
                article['_reasons'] = _get_article_reasons(conn, article, user_profile=profile)
                # Populate user_labels from junction table
                ulabels = conn.execute(
                    'SELECT label_name FROM article_user_labels WHERE article_id = ?', (article_id,)
                ).fetchall()
                article['user_labels'] = _sanitize_labels([l['label_name'] for l in ulabels])
                result.append(article)
        return result
    finally:
        conn.close()


def compute_article_scores(articles):
    """Given a list of article dicts, compute and attach _score + _reasons to each.
    Returns the list sorted by score DESC.
    """
    if not articles:
        return articles
    conn = get_db()
    try:
        profile = _build_user_profile(conn)
        for a in articles:
            a['_score'] = _get_article_score_internal(conn, a['id'], user_profile=profile)
            a['_reasons'] = _get_article_reasons(conn, a, user_profile=profile)
        articles.sort(key=lambda a: a.get('_score', 0), reverse=True)
        return articles
    finally:
        conn.close()


# ── Stats ──

def get_stats():
    """Return aggregate article statistics."""
    conn = get_db()
    try:
        total = conn.execute('SELECT COUNT(*) as cnt FROM articles').fetchone()['cnt']
        unread = conn.execute(
            'SELECT COUNT(*) as cnt FROM articles WHERE is_read = 0'
        ).fetchone()['cnt']
        read = conn.execute(
            'SELECT COUNT(*) as cnt FROM articles WHERE is_read = 1'
        ).fetchone()['cnt']

        label_rows = conn.execute(
            'SELECT label, COUNT(*) as cnt FROM articles WHERE label != "未分类" GROUP BY label ORDER BY cnt DESC'
        ).fetchall()
        by_label = {r['label']: r['cnt'] for r in label_rows}

        this_week = conn.execute(
            "SELECT COUNT(*) as cnt FROM articles WHERE created_at >= datetime('now', '-7 days')"
        ).fetchone()['cnt']

        avg_time = conn.execute(
            'SELECT AVG(read_time_min) as avg FROM articles'
        ).fetchone()['avg']
        avg_read_time = round(avg_time, 1) if avg_time else 0

        return {
            'total': total,
            'unread': unread,
            'read': read,
            'by_label': by_label,
            'this_week': this_week,
            'avg_read_time': avg_read_time,
        }
    finally:
        conn.close()


def get_user_profile_data_for_llm():
    """Export rich user behavior data for LLM to generate a deep interest profile.

    Returns structured data about the user's reading history, dismissals,
    snoozes, and preferences — designed to be fed into an LLM prompt.

    Returns None if the user has too little activity to build a meaningful profile.
    """
    conn = get_db()
    try:
        event_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM user_events"
        ).fetchone()['cnt']

        if event_count < 3:
            return None

        # Recently read articles (last 60 days, top 30)
        read_rows = conn.execute(
            """SELECT ar.title, ar.label, ar.read_time_display, ar.content_type,
                      ue.event_type, ue.created_at
               FROM user_events ue
               JOIN articles ar ON ue.article_id = ar.id
               WHERE ue.event_type IN ('read_done', 'opened')
                 AND ue.created_at >= datetime('now', '-60 days')
               ORDER BY ue.created_at DESC
               LIMIT 30"""
        ).fetchall()

        # Dismissed articles (last 60 days)
        dismiss_rows = conn.execute(
            """SELECT ar.title, ar.label, ue.created_at
               FROM user_events ue
               JOIN articles ar ON ue.article_id = ar.id
               WHERE ue.event_type = 'dismiss'
                 AND ue.created_at >= datetime('now', '-60 days')
               ORDER BY ue.created_at DESC
               LIMIT 15"""
        ).fetchall()

        # Snoozed articles (last 60 days)
        snooze_rows = conn.execute(
            """SELECT ar.title, ar.label, ar.snooze_until, ue.created_at
               FROM user_events ue
               JOIN articles ar ON ue.article_id = ar.id
               WHERE ue.event_type = 'snooze'
                 AND ue.created_at >= datetime('now', '-60 days')
               ORDER BY ue.created_at DESC
               LIMIT 10"""
        ).fetchall()

        # Per-label event summary (read vs dismiss vs snooze counts)
        label_summary = conn.execute(
            """SELECT ar.label,
                      SUM(CASE WHEN ue.event_type = 'read_done' THEN 1 ELSE 0 END) as read_cnt,
                      SUM(CASE WHEN ue.event_type = 'opened' THEN 1 ELSE 0 END) as opened_cnt,
                      SUM(CASE WHEN ue.event_type = 'dismiss' THEN 1 ELSE 0 END) as dismiss_cnt,
                      SUM(CASE WHEN ue.event_type = 'snooze' THEN 1 ELSE 0 END) as snooze_cnt
               FROM user_events ue
               JOIN articles ar ON ue.article_id = ar.id
               WHERE ar.label IS NOT NULL AND ar.label != '未分类' AND ar.label != '通知'
               GROUP BY ar.label
               ORDER BY read_cnt DESC"""
        ).fetchall()

        # Reading time distribution
        time_rows = conn.execute(
            """SELECT ar.read_time_min, COUNT(*) as cnt
               FROM user_events ue
               JOIN articles ar ON ue.article_id = ar.id
               WHERE ue.event_type IN ('read_done', 'opened')
                 AND ar.read_time_min IS NOT NULL
               GROUP BY ar.read_time_min
               ORDER BY cnt DESC
               LIMIT 10"""
        ).fetchall()

        # Article vs video preference
        type_rows = conn.execute(
            """SELECT ar.content_type, COUNT(*) as cnt
               FROM user_events ue
               JOIN articles ar ON ue.article_id = ar.id
               WHERE ue.event_type IN ('read_done', 'opened')
               GROUP BY ar.content_type"""
        ).fetchall()

        # Unread counts by label
        unread_by_label = conn.execute(
            """SELECT label, COUNT(*) as cnt
               FROM articles WHERE is_read = 0 AND label != '未分类'
               GROUP BY label ORDER BY cnt DESC"""
        ).fetchall()

        return {
            'event_count': event_count,
            'recently_read': [
                {
                    'title': r['title'],
                    'label': r['label'],
                    'duration': r['read_time_display'],
                    'type': r['content_type'],
                    'action': r['event_type'],
                }
                for r in read_rows
            ],
            'recently_dismissed': [
                {'title': r['title'], 'label': r['label']} for r in dismiss_rows
            ],
            'recently_snoozed': [
                {'title': r['title'], 'label': r['label']} for r in snooze_rows
            ],
            'label_summary': [
                {
                    'label': r['label'],
                    'read': r['read_cnt'],
                    'opened': r['opened_cnt'],
                    'dismissed': r['dismiss_cnt'],
                    'snoozed': r['snooze_cnt'],
                }
                for r in label_summary
            ],
            'reading_time_preferences': [
                {'minutes': r['read_time_min'], 'count': r['cnt']} for r in time_rows
            ],
            'content_type_preferences': [
                {'type': r['content_type'], 'count': r['cnt']} for r in type_rows
            ],
            'unread_by_label': [
                {'label': r['label'], 'count': r['cnt']} for r in unread_by_label
            ],
        }
    finally:
        conn.close()


def get_event_count():
    """Return total user event count — used for cache invalidation."""
    conn = get_db()
    try:
        return conn.execute("SELECT COUNT(*) as cnt FROM user_events").fetchone()['cnt']
    finally:
        conn.close()


def execute_raw(sql, params=None):
    """Execute a raw SELECT SQL query with safety assumption that caller validated it."""
    conn = get_db()
    try:
        rows = conn.execute(sql, params or []).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


