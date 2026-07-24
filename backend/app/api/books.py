from collections import Counter, defaultdict
from typing import Optional

from fastapi import APIRouter, Query, Depends
from sqlmodel import Session, select

from app.database import engine
from app.models import Book, Purchase

router = APIRouter(tags=["Books"])


def _clean_filter_values(values: list[str]) -> list[str]:
    """Normalize repeated query values while preserving their order."""
    return list(dict.fromkeys(
        value.strip() for value in values if value and value.strip()
    ))

# 取得 Database Session 的 Dependency
def get_session():
    with Session(engine) as session:
        yield session


@router.get("/filters")
def get_book_filter_options(
    user_id: str = Query(..., description="使用者 ID"),
    session: Session = Depends(get_session),
):
    """Return filter options derived only from this user's owned books."""
    purchases = session.exec(
        select(Purchase).where(Purchase.user_id == user_id)
    ).all()
    if not purchases:
        return {"total": 0, "platforms": [], "categories": []}

    owned_isbns = {purchase.isbn for purchase in purchases}
    books = session.exec(
        select(Book).where(Book.isbn.in_(owned_isbns))
    ).all()

    platform_isbns: dict[str, set[str]] = defaultdict(set)
    for purchase in purchases:
        platform_isbns[purchase.platform.lower()].add(purchase.isbn)

    category_counts = Counter(
        book.category or "未分類"
        for book in books
    )
    platform_labels = {"kobo": "Kobo", "readmoo": "Readmoo"}

    return {
        "total": len(books),
        "platforms": [
            {
                "value": platform,
                "label": platform_labels.get(platform, platform.title()),
                "count": len(isbns),
            }
            for platform, isbns in sorted(platform_isbns.items())
        ],
        "categories": [
            {"value": category, "label": category, "count": count}
            for category, count in sorted(
                category_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ],
    }


@router.get("/")
def get_user_books(
    user_id: str = Query(..., description="使用者 ID"),
    keyword: Optional[str] = Query(None, description="搜尋關鍵字（書名/作者）"),
    platform: list[str] = Query(
        default=[],
        description="平台多選，可重複傳入 (Readmoo/Kobo)",
    ),
    category: list[str] = Query(
        default=[],
        description="標準分類多選，可重複傳入",
    ),
    session: Session = Depends(get_session),
):
    selected_platforms = [
        value.lower() for value in _clean_filter_values(platform)
    ]
    selected_categories = _clean_filter_values(category)

    # Filter ownership in the database. Values within each group use OR;
    # platform and category groups are combined with AND.
    purchase_stmt = select(Purchase).where(Purchase.user_id == user_id)
    if selected_platforms:
        purchase_stmt = purchase_stmt.where(
            Purchase.platform.in_(selected_platforms)
        )

    purchases = session.exec(purchase_stmt).all()
    if not purchases:
        return []

    user_isbns = {purchase.isbn for purchase in purchases}

    book_stmt = select(Book).where(Book.isbn.in_(user_isbns))
    if selected_categories:
        book_stmt = book_stmt.where(Book.category.in_(selected_categories))

    if keyword:
        search_pattern = f"%{keyword.strip()}%"
        book_stmt = book_stmt.where(
            (Book.title.ilike(search_pattern)) |
            (Book.author.ilike(search_pattern))
        )

    books = session.exec(book_stmt.order_by(Book.title)).all()
    if not books:
        return []

    # Return every platform on which the user owns each matched book, not only
    # the platforms used to select it.
    matched_isbns = {book.isbn for book in books}
    all_matching_purchases = session.exec(
        select(Purchase).where(
            Purchase.user_id == user_id,
            Purchase.isbn.in_(matched_isbns),
        )
    ).all()
    platforms_by_isbn: dict[str, set[str]] = defaultdict(set)
    for purchase in all_matching_purchases:
        platforms_by_isbn[purchase.isbn].add(purchase.platform.lower())

    result = []
    for book in books:
        result.append({
            "isbn": book.isbn,
            "title": book.title,
            "author": book.author,
            "cover_url": book.cover_url,
            "category": book.category,
            "platforms": sorted(platforms_by_isbn[book.isbn]),
        })

    return result
