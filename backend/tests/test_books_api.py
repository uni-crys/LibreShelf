import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.books import get_book_filter_options, get_user_books
from app.models import Book, Purchase


class BookFilterApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add_all([
                Book(
                    isbn="book-a",
                    title="雙平台小說",
                    author="作者甲",
                    category="文學小說",
                ),
                Book(
                    isbn="book-b",
                    title="Kobo 社科",
                    author="作者乙",
                    category="人文社科",
                ),
                Book(
                    isbn="book-c",
                    title="Readmoo 小說",
                    author="作者丙",
                    category="文學小說",
                ),
                Purchase(
                    user_id="reader",
                    platform="kobo",
                    platform_book_id="ka",
                    isbn="book-a",
                ),
                Purchase(
                    user_id="reader",
                    platform="readmoo",
                    platform_book_id="ra",
                    isbn="book-a",
                ),
                Purchase(
                    user_id="reader",
                    platform="kobo",
                    platform_book_id="kb",
                    isbn="book-b",
                ),
                Purchase(
                    user_id="reader",
                    platform="readmoo",
                    platform_book_id="rc",
                    isbn="book-c",
                ),
            ])
            session.commit()

    def test_multiselect_uses_or_within_groups_and_and_between_groups(self):
        with Session(self.engine) as session:
            books = get_user_books(
                user_id="reader",
                keyword=None,
                platform=["kobo", "readmoo"],
                category=["文學小說"],
                session=session,
            )

        self.assertEqual(
            {book["isbn"] for book in books},
            {"book-a", "book-c"},
        )
        dual_platform_book = next(
            book for book in books if book["isbn"] == "book-a"
        )
        self.assertEqual(
            dual_platform_book["platforms"],
            ["kobo", "readmoo"],
        )

    def test_platform_and_category_filters_are_combined(self):
        with Session(self.engine) as session:
            books = get_user_books(
                user_id="reader",
                keyword=None,
                platform=["kobo"],
                category=["文學小說"],
                session=session,
            )

        self.assertEqual([book["isbn"] for book in books], ["book-a"])

    def test_multiple_categories_return_their_union(self):
        with Session(self.engine) as session:
            books = get_user_books(
                user_id="reader",
                keyword=None,
                platform=["kobo"],
                category=["文學小說", "人文社科"],
                session=session,
            )

        self.assertEqual(
            {book["isbn"] for book in books},
            {"book-a", "book-b"},
        )

    def test_filter_options_include_owned_book_counts(self):
        with Session(self.engine) as session:
            options = get_book_filter_options(
                user_id="reader",
                session=session,
            )

        self.assertEqual(
            {item["value"]: item["count"] for item in options["platforms"]},
            {"kobo": 2, "readmoo": 2},
        )
        self.assertEqual(
            {item["value"]: item["count"] for item in options["categories"]},
            {"文學小說": 2, "人文社科": 1},
        )
        self.assertEqual(options["total"], 3)


if __name__ == "__main__":
    unittest.main()
