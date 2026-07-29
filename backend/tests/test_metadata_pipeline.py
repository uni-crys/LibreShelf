import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services import metadata_pipeline
from app.services.metadata_pipeline import (
    MetadataCandidate,
    clean_title_for_search,
    extract_clean_author,
    is_valid_isbn,
    parse_books_detail,
    parse_books_search,
    parse_google_books_payload,
    parse_ncl_payload,
    parse_ncl_contributors,
    parse_ncl_detail,
    parse_ncl_search_results,
    parse_open_library_payload,
    parse_readmoo_detail,
    parse_readmoo_search,
    request_with_retry,
    score_candidate,
    split_classification,
    primary_ncl_title,
)


class MetadataParsingTests(unittest.TestCase):
    def test_isbn_checksums(self):
        self.assertTrue(is_valid_isbn("978-986-133-195-9"))
        self.assertTrue(is_valid_isbn("0-306-40615-2"))
        self.assertFalse(is_valid_isbn("9789861331956"))
        self.assertFalse(is_valid_isbn("0306406153"))

    def test_title_normalization_keeps_subtitle_and_non_marketing_edition(self):
        self.assertEqual(clean_title_for_search("書名：完整副標【獨家附贈】"), "書名：完整副標")
        self.assertEqual(clean_title_for_search("書名（增訂二版）：副標"), "書名（增訂二版）：副標")

    def test_title_normalization_tokenizes_middle_dot_and_plus(self):
        self.assertEqual(
            clean_title_for_search("親愛的夏吉‧班恩"),
            "親愛的夏吉 班恩",
        )
        self.assertEqual(
            clean_title_for_search("特別收錄後記＋相關事件地圖"),
            "特別收錄後記 相關事件地圖",
        )

    def test_author_normalization_removes_bilingual_alias(self):
        self.assertEqual(
            extract_clean_author("J.K.羅琳(J. K. Rowling)"),
            "J.K.羅琳",
        )
        self.assertEqual(
            extract_clean_author("埃莉諾.巴尼特(Eleanor Barnett)"),
            "埃莉諾‧巴尼特",
        )
        self.assertEqual(extract_clean_author("Jurafsky, Dan"), "Jurafsky, Dan")

    def test_ncl_text_category_takes_priority_over_classification_code(self):
        raw, codes, standard = split_classification([
            "431.9",
            "人文史地 (含哲學、宗教、史地、傳記、考古等)",
        ])
        self.assertEqual(
            raw,
            ["人文史地 (含哲學、宗教、史地、傳記、考古等)"],
        )
        self.assertEqual(codes, ["431.9"])
        self.assertEqual(standard, "人文社科")

    def test_readmoo_wellbeing_categories_map_to_psychology(self):
        raw, codes, standard = split_classification([
            "勵志成長",
            "情緒壓力",
        ])
        self.assertEqual(raw, ["勵志成長", "情緒壓力"])
        self.assertEqual(codes, [])
        self.assertEqual(standard, "心理勵志")

    def test_books_and_readmoo_top_level_categories_are_mapped(self):
        expected = {
            "商業理財": "商業理財",
            "電腦科技": "電腦資訊",
            "文學小說": "文學小說",
            "漫畫/圖文書": "漫畫/圖文",
            "藝術設計": "藝術設計",
            "社會科學": "人文社科",
            "宗教命理": "人文社科",
            "教育": "人文社科",
            "外國語言": "語言學習",
            "研究輔助": "考試用書",
            "勵志成長": "心理勵志",
            "自然科普": "自然科普",
            "醫療保健": "醫療保健",
            "飲食生活": "飲食料理",
            "生活風格": "生活風格",
            "旅遊觀光": "旅遊觀光",
            "青少年與兒童": "文學小說",
        }
        for raw_category, standard_category in expected.items():
            with self.subTest(raw_category=raw_category):
                self.assertEqual(
                    split_classification([raw_category])[2],
                    standard_category,
                )

    def test_parent_category_wins_before_ambiguous_child(self):
        self.assertEqual(
            split_classification(["商業理財", "傳記"])[2],
            "商業理財",
        )

    def test_ncl_parallel_title_keeps_main_title_for_display(self):
        original = (
            "不正義的地理學: 二戰後東亞的記憶戰爭與歷史裂痕"
            "= The genopathy of injustice"
        )
        self.assertEqual(
            primary_ncl_title(original),
            "不正義的地理學: 二戰後東亞的記憶戰爭與歷史裂痕",
        )

    def test_books_search_parser_returns_all_visible_candidates(self):
        html = """
        <table class="table-searchlist"><tbody>
          <tr><td><h3><a href="//books.example/a">甲書</a></h3>
            <p class="author">作者：甲</p><img data-src="http://img/a.jpg"></td></tr>
          <tr><td><h3><a href="//books.example/b">乙書</a></h3>
            <p class="author">作者：乙</p></td></tr>
        </tbody></table>
        """
        rows = parse_books_search(html)
        self.assertEqual([row["title"] for row in rows], ["甲書", "乙書"])
        self.assertEqual(rows[0]["detail_url"], "https://books.example/a")

    def test_books_detail_separates_category_and_code(self):
        html = """
        <div class="breadcrumb"><li><a>首頁</a></li><li><a>中文書</a></li>
          <li><a>心理勵志</a></li></div>
        <div>ISBN：9789861331959</div><a href="/publisher?pubid=1">測試出版</a>
        """
        parsed = parse_books_detail(html)
        self.assertEqual(parsed["raw_categories"], ["心理勵志"])
        self.assertEqual(parsed["identifiers"], ["9789861331959"])
        self.assertEqual(parsed["publisher"], "測試出版")

    def test_current_books_search_and_detail_parsers(self):
        search_html = """
        <div class="table-tr">
          <div class="table-td" id="prod-itemlist-0010863501">
            <div class="box"><img data-src="https://img.example/cover.jpg"></div>
            <h4><a title="哈利波特(1)：神秘的魔法石"
              href="//search.books.com.tw/redirect/move/key/x/area/mid_name/item/0010863501/page/1">
              哈利波特(1)：神秘的魔法石</a></h4>
            <p class="author">
              <a href="//search.books.com.tw/search/query/key/J.K./adv_author/1/">J.K.羅琳</a>
              <a href="//search.books.com.tw/search/query/key/Peng/adv_author/1/">彭倩文</a>
            </p>
          </div>
        </div>
        """
        rows = parse_books_search(search_html)
        self.assertEqual(rows[0]["source_id"], "0010863501")
        self.assertEqual(
            rows[0]["detail_url"],
            "https://www.books.com.tw/products/0010863501",
        )
        self.assertEqual(rows[0]["authors"], ["J.K.羅琳", "彭倩文"])

        western_html = """
        <div class="table-tr">
          <div class="table-td" id="prod-itemlist-F0123">
            <h4><a title="The Language of Food">The Language of Food</a></h4>
            <p class="author">
              <a href="/adv_author/1/">Dan</a>
              <a href="/adv_author/1/">Jurafsky</a>
            </p>
          </div>
        </div>
        """
        self.assertEqual(
            parse_books_search(western_html)[0]["authors"],
            ["Dan Jurafsky"],
        )

        detail_html = """
        <ul id="breadcrumb-trail" typeof="BreadcrumbList">
          <li><a>博客來</a></li><li><a>中文書</a></li>
          <li><a>文學小說</a></li><li><a>科幻/奇幻小說</a></li>
        </ul>
        <div class="type02_p003"><ul>
          <li>作者：<a href="/search/adv_author/1/">J.K.羅琳</a></li>
          <li>譯者：<a href="/search/adv_author/1/">彭倩文</a></li>
          <li>出版社：<a href="/web/sys_puballb/books/?pubid=crown">皇冠</a></li>
        </ul></div>
        <li>ISBN：9789573335566</li>
        <script type="application/ld+json">
        {"@type":"Book","author":[{"name":"J.K.羅琳"}],
         "publisher":[{"name":"皇冠"}],
         "workExample":{"workExample":{"isbn":"9789573335566"}}}
        </script>
        """
        parsed = parse_books_detail(detail_html)
        self.assertEqual(parsed["raw_categories"], ["文學小說", "科幻/奇幻小說"])
        self.assertEqual(parsed["identifiers"], ["9789573335566"])
        self.assertEqual(
            [(person.name, person.role) for person in parsed["contributors"]],
            [("J.K.羅琳", "作者"), ("彭倩文", "譯者")],
        )

    def test_readmoo_search_preserves_roles(self):
        html = """
        <li class="listItem-box" data-id="book-1"><h3><a>測試書</a></h3>
          <div class="contributor-info">
            <a aria-label="作者">王小明</a><a aria-label="譯者">陳小華</a>
            <a aria-label="繪者">林小美</a>
          </div><img class="js-lazy-image" data-lazy-original="cover.jpg">
        </li>
        """
        candidate = parse_readmoo_search(html)[0]
        self.assertEqual(
            [(item.name, item.role) for item in candidate.contributors],
            [("王小明", "作者"), ("陳小華", "譯者"), ("林小美", "繪者")],
        )

    def test_current_readmoo_search_parser(self):
        html = """
        <div id="main_items" class="rm-book-thumbnails">
          <li class="listItem-box">
            <img class="js-lazy-image" data-lazy-original="https://img.example/book.jpg">
            <div class="caption">
              <h4><a class="product-link" data-readmoo-id="210400607000101"
                href="https://readmoo.com/book/210400607000101"
                title="哈利波特(1) 神秘的魔法石">哈利波特(1) 神秘的魔法石</a></h4>
              <div class="contributor-info">
                <a aria-label="作者 J.K.羅琳">J.K.羅琳</a>
                <a aria-label="譯者 彭倩文">彭倩文</a>
              </div>
              <div class="publisher-info"><a>皇冠文化有限公司</a></div>
              <meta itemprop="identifier" content="210400607000101">
            </div>
          </li>
        </div>
        """
        candidate = parse_readmoo_search(html)[0]
        self.assertEqual(candidate.source_id, "210400607000101")
        self.assertEqual(candidate.publisher, "皇冠文化有限公司")
        self.assertEqual(
            [(person.name, person.role) for person in candidate.contributors],
            [("J.K.羅琳", "作者"), ("彭倩文", "譯者")],
        )

    def test_current_readmoo_detail_parser(self):
        html = """
        <ol class="breadcrumb">
          <li><a>分類導覽</a></li><li><a>文學小說</a></li><li><a>散文</a></li>
        </ol>
        <h1 class="book-detail-title" itemprop="name">在小山和小山之間</h1>
        <div class="book-meta">
          <ul class="book-meta-author">
            <li>作者：<a itemprop="name" href="/contributor/1">李停</a></li>
            <li>譯者：<a itemprop="name" href="/contributor/2">王小明</a></li>
            <li>出版社：<a itemprop="publisher" href="/publisher/1">悅知文化</a></li>
          </ul>
          <ul class="book-meta-published">
            <li>ISBN: <span itemprop="isbn">9786267721834</span></li>
            <li>eISBN: <span itemprop="eisbn">9786267721841</span></li>
          </ul>
        </div>
        """
        parsed = parse_readmoo_detail(html)
        self.assertEqual(parsed["title"], "在小山和小山之間")
        self.assertEqual(parsed["raw_categories"], ["文學小說", "散文"])
        self.assertEqual(
            parsed["identifiers"],
            ["9786267721834", "9786267721841"],
        )
        self.assertEqual(
            [(person.name, person.role) for person in parsed["contributors"]],
            [("李停", "作者"), ("王小明", "譯者")],
        )

    def test_ncl_parser_separates_classification_number(self):
        candidate = parse_ncl_payload([{
            "書名": "測試書", "作者": "王小明", "譯者": "陳小華",
            "分類號": "563.5", "isbn": "9789861331959",
        }])[0]
        self.assertEqual(candidate.category_codes, ["563.5"])
        self.assertEqual(candidate.raw_categories, [])
        self.assertEqual(candidate.contributors[1].role, "譯者")

    def test_current_ncl_html_parsers(self):
        search_html = """
        <table><tr><th></th><th></th><th>書名</th><th>作者</th><th>出版者</th></tr>
        <tr><td>1</td><td></td><td>
          <a href="main_DisplayRecord.php?&Pact=init&Pstart=1">少年粉紅</a>
        </td><td>潘柏霖作</td><td>尖端</td></tr></table>
        """
        detail_html = """
        <table>
          <tr><td>書名</td><td>少年粉紅 = Young Pink</td></tr>
          <tr><td>作者</td><td>潘柏霖作；王小明譯</td></tr>
          <tr><td>出版機構</td><td>尖端</td></tr>
          <tr><td>出版版次</td><td>1版</td></tr>
          <tr><td>圖書類號</td><td>857.7</td></tr>
          <tr><td>建議上架分類</td><td>小說</td></tr>
        </table>
        <table><tr><td>ISBN(裝訂方式)</td><td>9789571078304 (平裝)</td></tr></table>
        """
        rows = parse_ncl_search_results(search_html)
        self.assertEqual(rows[0]["title"], "少年粉紅")
        candidate = parse_ncl_detail(detail_html, queried_isbn="9789571078304")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.title, "少年粉紅")
        self.assertEqual(candidate.original_title, "少年粉紅 = Young Pink")
        self.assertEqual(candidate.category_codes, ["857.7"])
        self.assertEqual(candidate.raw_categories, ["小說"])
        self.assertEqual(
            [(person.name, person.role) for person in candidate.contributors],
            [("潘柏霖", "作者"), ("王小明", "譯者")],
        )
        self.assertEqual(candidate.identifiers, ["9789571078304"])

    def test_ncl_contributor_roles(self):
        people = parse_ncl_contributors("作者甲著；譯者乙翻譯；繪者丙繪")
        self.assertEqual(
            [(person.name, person.role) for person in people],
            [("作者甲", "作者"), ("譯者乙", "譯者"), ("繪者丙", "繪者")],
        )

    def test_google_checks_identifiers_on_every_item(self):
        payload = {"items": [
            {"id": "wrong", "volumeInfo": {
                "title": "同名書", "industryIdentifiers": [{"identifier": "9780306406157"}],
            }},
            {"id": "right", "volumeInfo": {
                "title": "同名書", "industryIdentifiers": [{"identifier": "9789861331959"}],
            }},
        ]}
        candidates = parse_google_books_payload(payload)
        scores = [
            score_candidate(item, isbn="9789861331959", title="同名書")
            for item in candidates
        ]
        self.assertGreater(scores[1], scores[0])
        self.assertIn("isbn", candidates[1].matched_by)
        self.assertIn("isbn_conflict", candidates[0].matched_by)

    def test_google_combines_title_and_subtitle(self):
        candidates = parse_google_books_payload({"items": [{
            "id": "food",
            "volumeInfo": {
                "title": "The Language of Food",
                "subtitle": "A Linguist Reads the Menu",
                "industryIdentifiers": [{"identifier": "9780393351620"}],
            },
        }]})
        self.assertEqual(
            candidates[0].title,
            "The Language of Food: A Linguist Reads the Menu",
        )

    def test_open_library_parser_keeps_exact_edition(self):
        payload = {
            "records": {
                "/books/OL28567315M": {
                    "isbns": ["9780393351620"],
                    "recordURL": "http://openlibrary.org/books/OL28567315M",
                    "data": {
                        "key": "/books/OL28567315M",
                        "title": "Language of Food",
                        "subtitle": "A Linguist Reads the Menu",
                        "authors": [{"name": "Dan Jurafsky"}],
                        "publishers": [{"name": "W. W. Norton"}],
                        "subjects": [
                            {"name": "Food, history"},
                            {"name": "Food habits"},
                        ],
                        "identifiers": {"isbn_13": ["9780393351620"]},
                    },
                },
            },
            "items": [],
        }
        candidate = parse_open_library_payload(payload)[0]
        self.assertEqual(
            candidate.title,
            "Language of Food: A Linguist Reads the Menu",
        )
        self.assertEqual(candidate.identifiers, ["9780393351620"])
        self.assertEqual(candidate.contributors[0].name, "Dan Jurafsky")
        self.assertEqual(
            split_classification(candidate.raw_categories)[2],
            "生活風格",
        )

    def test_title_only_candidate_can_reach_selection_threshold(self):
        candidate = MetadataCandidate(source="test", title="精準書名：完整副標")
        score = score_candidate(
            candidate, isbn=None, title="精準書名：完整副標"
        )
        self.assertGreaterEqual(score, 0.48)

    def test_source_without_isbn_can_match_by_exact_title(self):
        candidate = MetadataCandidate(source="readmoo", title="精準書名")
        score = score_candidate(
            candidate,
            isbn="9789861331959",
            title="精準書名",
        )
        self.assertGreaterEqual(score, 0.58)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_429_then_success(self):
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(429 if attempts == 1 else 200, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await request_with_retry(client, "https://example.test", attempts=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, 2)

    async def test_google_429_opens_circuit_breaker(self):
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(429, request=request)

        metadata_pipeline._source_cooldowns.clear()
        metadata_pipeline._source_failures.clear()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await metadata_pipeline.fetch_from_google_books(client, "測試")
            first_attempts = attempts
            second = await metadata_pipeline.fetch_from_google_books(client, "另一本書")

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(first_attempts, 2)
        self.assertEqual(attempts, first_attempts)
        metadata_pipeline._source_cooldowns.clear()
        metadata_pipeline._source_failures.clear()

    async def test_books_transport_failure_opens_circuit_breaker(self):
        client = object()
        metadata_pipeline._source_cooldowns.clear()
        metadata_pipeline._source_failures.clear()
        request = AsyncMock(side_effect=httpx.ReadError("connection reset"))

        with patch.object(metadata_pipeline, "request_with_retry", request):
            with self.assertRaises(httpx.ReadError):
                await metadata_pipeline.fetch_from_books_com(client, "第一本")
            second = await metadata_pipeline.fetch_from_books_com(client, "第二本")

        self.assertEqual(second, [])
        request.assert_awaited_once()
        metadata_pipeline._source_cooldowns.clear()
        metadata_pipeline._source_failures.clear()

    async def test_readmoo_uses_current_query_parameter(self):
        captured_query = None

        def handler(request):
            nonlocal captured_query
            captured_query = request.url.params
            return httpx.Response(200, request=request, text="<html></html>")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await metadata_pipeline.fetch_from_readmoo_web(client, "測試書")

        self.assertEqual(captured_query.get("q"), "測試書")
        self.assertEqual(captured_query.get("page"), "1")


class SourcePriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_kobo_platform_fallback_precedes_open_library_and_google(self):
        with (
            patch.object(
                metadata_pipeline,
                "get_shared_client",
                AsyncMock(return_value=object()),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_books_com",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_readmoo_web",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_open_library",
                AsyncMock(return_value=[]),
            ) as open_library,
            patch.object(
                metadata_pipeline,
                "fetch_from_google_books",
                AsyncMock(return_value=[]),
            ) as google,
        ):
            result = await metadata_pipeline._lookup_metadata(
                "kobo-product-id",
                "平台書籍名稱",
                None,
                None,
                None,
                {
                    "source": "kobo",
                    "title": "平台書籍名稱",
                    "author": "平台作者",
                    "category": "社會科學",
                    "cover_url": "https://example.test/kobo.jpg",
                },
            )

        open_library.assert_not_awaited()
        google.assert_not_awaited()
        self.assertEqual(result["source"], "kobo")
        self.assertEqual(result["standard_category"], "人文社科")

    async def test_reliable_ncl_match_stops_fallback_chain(self):
        ncl_candidate = MetadataCandidate(
            source="ncl",
            title="測試書",
            identifiers=["9789861331959"],
            raw_categories=["文學小說"],
        )
        with (
            patch.object(
                metadata_pipeline,
                "get_shared_client",
                AsyncMock(return_value=object()),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_ncl",
                AsyncMock(return_value=[ncl_candidate]),
            ) as ncl,
            patch.object(
                metadata_pipeline,
                "fetch_from_books_com",
                AsyncMock(return_value=[]),
            ) as books,
            patch.object(
                metadata_pipeline,
                "fetch_from_google_books",
                AsyncMock(return_value=[]),
            ) as google,
            patch.object(
                metadata_pipeline,
                "fetch_from_readmoo_web",
                AsyncMock(return_value=[]),
            ) as readmoo,
        ):
            result = await metadata_pipeline._lookup_metadata(
                "9789861331959", "測試書", None, None, None
            )

        ncl.assert_awaited_once()
        books.assert_not_awaited()
        google.assert_not_awaited()
        readmoo.assert_not_awaited()
        self.assertEqual(result["source"], "ncl")

    async def test_exact_ncl_without_category_continues_and_merges_readmoo(self):
        ncl_candidate = MetadataCandidate(
            source="ncl",
            title="舌尖上的香料史",
            identifiers=["9786267558935"],
            contributors=[metadata_pipeline.Contributor("伊恩・安德森")],
        )
        readmoo_candidate = MetadataCandidate(
            source="readmoo",
            title="舌尖上的香料史",
            identifiers=["9786267558935"],
            raw_categories=["人文社科"],
        )
        with (
            patch.object(
                metadata_pipeline,
                "get_shared_client",
                AsyncMock(return_value=object()),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_ncl",
                AsyncMock(return_value=[ncl_candidate]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_books_com",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_readmoo_web",
                AsyncMock(return_value=[readmoo_candidate]),
            ) as readmoo,
            patch.object(
                metadata_pipeline,
                "fetch_from_open_library",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_google_books",
                AsyncMock(return_value=[]),
            ) as google,
        ):
            result = await metadata_pipeline._lookup_metadata(
                "9786267558935",
                "舌尖上的香料史",
                None,
                None,
                None,
            )

        readmoo.assert_awaited_once()
        google.assert_not_awaited()
        self.assertEqual(result["source"], "ncl")
        self.assertEqual(result["author"], "伊恩・安德森")
        self.assertEqual(result["category"], "人文社科")

    async def test_unverified_books_result_falls_through_to_open_library(self):
        books_candidate = MetadataCandidate(
            source="books.com.tw",
            title="The Language of Food",
        )
        open_library_candidate = MetadataCandidate(
            source="open_library",
            title="The Language of Food: A Linguist Reads the Menu",
            identifiers=["9780393351620"],
            contributors=[metadata_pipeline.Contributor("Dan Jurafsky")],
            raw_categories=["Food habits"],
        )
        with (
            patch.object(
                metadata_pipeline,
                "get_shared_client",
                AsyncMock(return_value=object()),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_ncl",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_books_com",
                AsyncMock(return_value=[books_candidate]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_readmoo_web",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_open_library",
                AsyncMock(return_value=[open_library_candidate]),
            ) as open_library,
            patch.object(
                metadata_pipeline,
                "fetch_from_google_books",
                AsyncMock(return_value=[]),
            ) as google,
        ):
            result = await metadata_pipeline._lookup_metadata(
                "9780393351620",
                "The Language of Food: A Linguist Reads the Menu",
                None,
                None,
                None,
            )

        open_library.assert_awaited_once()
        google.assert_not_awaited()
        self.assertEqual(result["source"], "open_library")
        self.assertEqual(result["category"], "生活風格")

    async def test_books_detail_403_falls_through_for_platform_id(self):
        books_candidate = MetadataCandidate(
            source="books.com.tw",
            title="為什麼魚不存在：關於失去、愛與生命的本質，踏上追尋人生意義的解答之旅",
            detail_status=403,
        )
        readmoo_candidate = MetadataCandidate(
            source="readmoo",
            title="為什麼魚不存在",
            raw_categories=["文學小說"],
        )
        with (
            patch.object(
                metadata_pipeline,
                "get_shared_client",
                AsyncMock(return_value=object()),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_books_com",
                AsyncMock(return_value=[books_candidate]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_readmoo_web",
                AsyncMock(return_value=[readmoo_candidate]),
            ) as readmoo,
            patch.object(
                metadata_pipeline,
                "fetch_from_open_library",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_google_books",
                AsyncMock(return_value=[]),
            ) as google,
        ):
            result = await metadata_pipeline._lookup_metadata(
                "platform-id",
                "為什麼魚不存在：關於失去、愛與生命的本質，踏上追尋人生意義的解答之旅",
                None,
                None,
                None,
            )

        readmoo.assert_awaited_once()
        google.assert_not_awaited()
        self.assertEqual(result["source"], "readmoo")
        self.assertEqual(result["category"], "文學小說")

    async def test_readmoo_match_does_not_spend_google_quota(self):
        readmoo_candidate = MetadataCandidate(
            source="readmoo",
            title="平台書籍名稱",
            raw_categories=["文學小說"],
        )
        with (
            patch.object(
                metadata_pipeline,
                "get_shared_client",
                AsyncMock(return_value=object()),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_books_com",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_readmoo_web",
                AsyncMock(return_value=[readmoo_candidate]),
            ) as readmoo,
            patch.object(
                metadata_pipeline,
                "fetch_from_google_books",
                AsyncMock(return_value=[]),
            ) as google,
        ):
            result = await metadata_pipeline._lookup_metadata(
                "platform-id", "平台書籍名稱", None, None, None
            )

        readmoo.assert_awaited_once()
        google.assert_not_awaited()
        self.assertEqual(result["source"], "readmoo")

    async def test_source_transport_failure_does_not_abort_pipeline(self):
        readmoo_candidate = MetadataCandidate(
            source="readmoo",
            title="連線失敗後仍可找到",
            raw_categories=["文學小說"],
        )
        with (
            patch.object(
                metadata_pipeline,
                "get_shared_client",
                AsyncMock(return_value=object()),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_books_com",
                AsyncMock(side_effect=httpx.ReadError("connection reset")),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_readmoo_web",
                AsyncMock(return_value=[readmoo_candidate]),
            ),
            patch.object(
                metadata_pipeline,
                "fetch_from_google_books",
                AsyncMock(return_value=[]),
            ) as google,
        ):
            result = await metadata_pipeline._lookup_metadata(
                "platform-id", "連線失敗後仍可找到", None, None, None
            )

        google.assert_not_awaited()
        self.assertEqual(result["source"], "readmoo")


if __name__ == "__main__":
    unittest.main()
