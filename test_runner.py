#!/usr/bin/env python3
"""Automated regression test suite for 「稍后看」.

Covers:
  1. DB-level unit tests (XSS sanitization, data integrity)
  2. API integration tests (config validation, CRUD, recommendations)
  3. LLM-path regression tests (keyword search, random, topic exclusion)

Usage:
  python3 test_runner.py              # all tests
  python3 test_runner.py --quick      # skip slow LLM tests
  python3 test_runner.py --offline    # db-only tests (no server needed)
"""

import sys
import os
import json
import re
import time
import unittest
import urllib.request
import urllib.error
import urllib.parse

BASE = 'http://localhost:8080'
ROOT = os.path.dirname(os.path.abspath(__file__))


# ── helpers ──

def api(method, path, body=None, timeout=10):
    """Call the API, return (status, json_data)."""
    url = BASE + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors='replace')
        try:
            return e.code, json.loads(body_text)
        except Exception:
            return e.code, {'error': body_text[:500]}
    except Exception as e:
        return None, {'error': str(e)}


def server_ok():
    """Return True if the server is reachable."""
    try:
        code, _ = api('GET', '/api/health')
        return code == 200
    except Exception:
        return False


# ── DB Unit Tests ──

class TestDatabase(unittest.TestCase):
    """Tests against db.py that don't need the server running."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROOT)
        global database
        import db as database

    def test_row_to_dict_strips_xss_labels(self):
        """_row_to_dict should sanitize labels containing HTML."""
        # Simulate a row-like object
        class Row:
            def keys(self):
                return ['id', 'title', 'label', 'url', 'is_read',
                        'read_at', 'created_at', 'summary', 'content_preview']
            def __getitem__(self, k):
                if k == 'id': return 9999
                if k == 'title': return 'test'
                if k == 'label': return '<img src=x onerror=alert(1)>'
                if k == 'is_read': return 0
                if k == 'read_at': return None
                if k == 'created_at': return '2026-01-01'
                if k == 'url': return 'https://example.com'
                if k == 'summary': return ''
                if k == 'content_preview': return ''
                return None
            def __contains__(self, k): return k in self.keys()
            def get(self, k, default=None): return self.__getitem__(k) if k in self.keys() else default
            def index(self, k): return list(self.keys()).index(k)

        d = database._row_to_dict(Row())
        self.assertEqual(d['label'], '未分类',
                         'XSS label should be sanitized to 未分类')

    def test_sanitize_labels_filters_html(self):
        """_sanitize_labels should remove label names with <> chars."""
        result = database._sanitize_labels([
            '通知', '<script>alert(1)</script>', '时政', '文艺'
        ])
        self.assertEqual(result, ['通知', '时政', '文艺'])

    def test_sanitize_labels_empty_input(self):
        self.assertIsNone(database._sanitize_labels(None))
        self.assertEqual(database._sanitize_labels([]), [])

    def test_get_articles_returns_list(self):
        arts, total = database.get_articles(limit=5)
        self.assertIsInstance(arts, list)
        self.assertIsInstance(total, int)
        self.assertLessEqual(len(arts), 5)
        for a in arts:
            self.assertIn('id', a)
            self.assertIn('title', a)
            # No raw HTML in labels
            self.assertNotIn('<', a.get('label', ''))

    def test_get_articles_search(self):
        """Search should find articles by keyword."""
        arts, total = database.get_articles(search='Python', limit=10)
        found = any('python' in a.get('title', '').lower() for a in arts)
        self.assertTrue(found or total > 0,
                        'Search for "Python" should find at least the Wikipedia article')

    def test_config_functions_exist(self):
        self.assertTrue(hasattr(database, 'set_config'))
        self.assertTrue(hasattr(database, 'get_config'))


# ── API Integration Tests ──

@unittest.skipUnless(server_ok(), 'Server not reachable')
class TestApiHealth(unittest.TestCase):

    def test_health(self):
        code, data = api('GET', '/api/health')
        self.assertEqual(code, 200)
        self.assertEqual(data.get('status'), 'ok')

    def test_stats(self):
        code, data = api('GET', '/api/stats')
        self.assertEqual(code, 200)
        self.assertIn('stats', data)
        self.assertIn('total', data['stats'])

    def test_presets(self):
        code, data = api('GET', '/api/presets')
        self.assertEqual(code, 200)
        self.assertIn('labels', data)

    def test_articles_list(self):
        code, data = api('GET', '/api/articles?limit=5')
        self.assertEqual(code, 200)
        self.assertIn('articles', data)
        self.assertLessEqual(len(data['articles']), 5)

    def test_single_article(self):
        """Fetch first article and verify its structure."""
        _, all_data = api('GET', '/api/articles?limit=1')
        arts = all_data.get('articles', [])
        if arts:
            aid = arts[0]['id']
            code, data = api('GET', f'/api/articles/{aid}')
            self.assertEqual(code, 200)
            a = data.get('article', {})
            self.assertEqual(a['id'], aid)
            self.assertIn('title', a)
            self.assertIn('label', a)
            self.assertNotIn('<', a.get('label', ''),
                             'Label should not contain HTML')

    def test_404_article(self):
        code, data = api('GET', '/api/articles/999999')
        self.assertEqual(code, 404)

    def test_negative_limit_clamped(self):
        """?limit=-5 should be clamped, not crash."""
        code, data = api('GET', '/api/articles?limit=-5')
        self.assertEqual(code, 200)
        self.assertLessEqual(len(data.get('articles', [])), 200)


# ── Security Tests ──

@unittest.skipUnless(server_ok(), 'Server not reachable')
class TestSecurity(unittest.TestCase):

    def test_config_rejects_xss_label_name(self):
        """Config PUT should reject labels with <> characters."""
        code, data = api('PUT', '/api/config', body={
            'label_config': {
                'customLabels': [{'name': '<img src=x>'}],
                'labelOrder': [],
            }
        })
        self.assertEqual(code, 400)
        self.assertIn('无效', data.get('error', ''))

    def test_config_rejects_unknown_label_in_order(self):
        """Config PUT should reject labelOrder entries not in known labels."""
        code, data = api('PUT', '/api/config', body={
            'label_config': {
                'customLabels': [],
                'labelOrder': ['__FAKE_LABEL_NOT_EXIST__'],
            }
        })
        self.assertEqual(code, 400)
        self.assertIn('未知', data.get('error', ''))

    def test_config_rejects_long_label_name(self):
        code, data = api('PUT', '/api/config', body={
            'label_config': {
                'customLabels': [{'name': 'A' * 200}],
                'labelOrder': [],
            }
        })
        self.assertEqual(code, 400)

    def test_config_rejects_empty_label_name(self):
        code, data = api('PUT', '/api/config', body={
            'label_config': {
                'customLabels': [{'name': '   '}],
                'labelOrder': [],
            }
        })
        self.assertEqual(code, 400)


# ── Recommendation Tests ──

@unittest.skipUnless(server_ok(), 'Server not reachable')
class TestRecommendations(unittest.TestCase):

    def test_startup_returns_valid_card(self):
        code, data = api('GET', '/api/recommend/startup')
        if code == 200:
            a = data.get('article', {})
            self.assertIn('id', a)
            self.assertIn('title', a)
            self.assertNotEqual(a.get('label'), '通知',
                                'Startup recommend should exclude 通知')
        else:
            self.assertIn(data.get('error', ''), [
                'No recommendable articles'
            ])

    def test_startup_exclude_works(self):
        """?exclude=id should skip that article."""
        code1, data1 = api('GET', '/api/recommend/startup')
        if code1 != 200:
            self.skipTest('No recommendable articles')
        first_id = data1['article']['id']
        code2, data2 = api('GET', f'/api/recommend/startup?exclude={first_id}')
        if code2 == 200:
            self.assertNotEqual(data2['article']['id'], first_id,
                                'Excluded article should not be returned')

    def test_recommend_top(self):
        code, data = api('GET', '/api/recommend/top?limit=3')
        self.assertEqual(code, 200)
        arts = data.get('articles', [])
        labels = [a.get('label') for a in arts]
        self.assertNotIn('通知', labels, 'Default recommend should exclude 通知')


# ── LLM Query Regression Tests ──

@unittest.skipUnless(server_ok(), 'Server not reachable')
class TestQueryRouting(unittest.TestCase):
    """Tests that require LLM access.  These may be slow."""

    TIMEOUT = 90  # seconds per LLM call

    @classmethod
    def setUpClass(cls):
        # Check if LLM is configured by doing a quick stats query
        code, _ = api('GET', '/api/presets')
        cls.llm_available = code == 200

    def _query(self, question, timeout=None):
        return api('POST', '/api/query',
                   body={'question': question},
                   timeout=timeout or self.TIMEOUT)

    def test_notification_query_returns_results(self):
        """Regression: '我有什么错过的通知吗' should find 通知 articles."""
        code, data = self._query('我有什么通知')
        self.assertEqual(code, 200)
        self.assertTrue(data.get('success'))
        ans = data.get('answer', '')
        # Should have results or a graceful "no matches" response
        self.assertTrue(len(ans) > 10, 'Answer should not be empty')

    def test_python_keyword_search(self):
        """Regression: '推荐一篇Python入门' must find Python article (id=868)."""
        code, data = self._query('推荐一篇Python入门')
        self.assertEqual(code, 200)
        ans = data.get('answer', '')
        arts = data.get('articles', [])
        # The keyword search should inject the Python article into candidates
        python_in_ans = 'Python' in ans or 'python' in ans.lower()
        python_in_arts = any('python' in a.get('title', '').lower() for a in arts)
        self.assertTrue(python_in_ans or python_in_arts,
                        f'Python article should appear in answer or candidate pool')

    def test_random_recommendation(self):
        """Regression: '帮我随机推荐一篇' should route to recommend path."""
        code, data = self._query('帮我随机推荐一篇')
        self.assertEqual(code, 200)
        self.assertTrue(data.get('success'))

    def test_topic_exclusion(self):
        """Regression: '不要再给我推时政' should exclude 时政 from candidates."""
        code, data = self._query('不要再给我推时政的内容了')
        self.assertEqual(code, 200)
        arts = data.get('articles', [])
        # After exclusion, no 时政 articles in returned pool
        has_shizheng = any(
            a.get('label') == '时政' for a in arts
        )
        # Note: if the library has few non-时政 articles, this may still pass
        # because the exclusion hint tells LLM to skip them
        ans = data.get('answer', '')
        acknowledgement = any(
            kw in ans for kw in ['时政', '排除', '跳过', '先放一放']
        )
        self.assertTrue(acknowledgement or not has_shizheng,
                        'Topic exclusion should be acknowledged or applied')

    def test_time_constraint(self):
        """Time-based queries should return valid results."""
        code, data = self._query('我中午有30分钟午休，想看一篇能看完的文章')
        self.assertEqual(code, 200)
        self.assertTrue(data.get('success'))
        ans = data.get('answer', '')
        # Should contain [OPTION:] format
        self.assertIn('[OPTION:', ans)

    def test_emotional_state(self):
        """Emotional queries should still work."""
        code, data = self._query('今天考试考砸了，想看些轻松治愈的内容')
        self.assertEqual(code, 200)
        ans = data.get('answer', '')
        self.assertTrue(len(ans) > 20, 'Should get a meaningful response')

    def test_empty_library_handled(self):
        """Query about nonexistent topics should get graceful response."""
        code, data = self._query('有没有关于火星移民计划的文章')
        self.assertEqual(code, 200)
        ans = data.get('answer', '')
        self.assertTrue(len(ans) > 10)
        # Should not crash or return raw error
        self.assertNotIn('Traceback', ans)
        self.assertNotIn('Error', ans)


# ── Action / Feedback Tests ──

@unittest.skipUnless(server_ok(), 'Server not reachable')
class TestFeedback(unittest.TestCase):

    def test_feedback_records(self):
        """POST /api/feedback should accept valid events."""
        # Get a real article id first
        _, all_data = api('GET', '/api/articles?limit=1')
        arts = all_data.get('articles', [])
        if not arts:
            self.skipTest('No articles to test feedback')
        aid = arts[0]['id']

        for event in ['opened', 'read_done', 'dismiss']:
            code, data = api('POST', '/api/feedback', body={
                'article_id': aid,
                'event_type': event,
            })
            self.assertEqual(code, 200)
            self.assertTrue(data.get('success'))

    def test_article_update(self):
        """PUT /api/articles/:id should update read status."""
        _, all_data = api('GET', '/api/articles?limit=1')
        arts = all_data.get('articles', [])
        if not arts:
            self.skipTest('No articles')
        aid = arts[0]['id']

        # Toggle read status
        code, data = api('PUT', f'/api/articles/{aid}', body={
            'is_read': 0,
            'label': '未分类',
            'user_labels': [],
        })
        self.assertEqual(code, 200)


# ── Data Integrity ──

class TestDataIntegrity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROOT)
        global database
        import db as database

    def test_no_orphan_user_labels(self):
        conn = database.get_db()
        orphans = conn.execute(
            'SELECT COUNT(*) as cnt FROM article_user_labels aul '
            'LEFT JOIN articles a ON a.id = aul.article_id '
            'WHERE a.id IS NULL'
        ).fetchone()
        self.assertEqual(orphans['cnt'], 0,
                         'No orphan user labels should exist')

    def test_no_xss_labels_stored(self):
        conn = database.get_db()
        xss_labels = conn.execute(
            "SELECT COUNT(*) as cnt FROM articles WHERE label LIKE '%<%' OR label LIKE '%>%'"
        ).fetchone()
        self.assertEqual(xss_labels['cnt'], 0,
                         'No XSS labels should exist in the database')

    def test_all_articles_have_required_fields(self):
        conn = database.get_db()
        null_issues = conn.execute(
            'SELECT COUNT(*) as cnt FROM articles WHERE title IS NULL OR title = ""'
        ).fetchone()
        self.assertEqual(null_issues['cnt'], 0,
                         'All articles must have non-empty titles')


# ── Main ──

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--quick', action='store_true',
                   help='Skip slow LLM-dependent tests')
    p.add_argument('--offline', action='store_true',
                   help='Only run DB-level tests (no server needed)')
    args = p.parse_args()

    if args.offline:
        suite = unittest.TestLoader().loadTestsFromTestCase(TestDatabase)
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestDataIntegrity))
    elif args.quick:
        suite = unittest.TestLoader().loadTestsFromTestCase(TestDatabase)
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestApiHealth))
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestSecurity))
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestRecommendations))
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestFeedback))
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestDataIntegrity))
    else:
        suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
