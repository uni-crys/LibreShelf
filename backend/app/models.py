from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship
from datetime import datetime

class Book(SQLModel, table=True):
    """the intergrated books info cache table"""
    __tablename__ = "sttandard_books" # the table name in the database

    isbn:str = Field(primary_key = True)
    title: str = Field(index=True) # 書名
    author: Optional[str] = None # 作者
    cover_url: Optional[str] = None # 封面圖片 URL
    category: str = Field(default="Unkown")
    updated_at: datetime = Field(default_factory=datetime.utcnow) # the last update time of the book info

    purchases: List["Purchase"] = Relationship(back_populates="book")
    whishlist_items: List["WishlistItem"] = Relationship(back_populates="book", sa_relationship_kwargs={"cascade": "delete"})

class Purchase(SQLModel, table=True):
    __tablename__ = "user_purchases" # the table name in the database
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True) # the user id
    platform: str # Readmoo or Kobo currently supported
    platform_book_id: Optional[str] # the book id in the platform, for example, Readmoo's book id or Kobo's book id
    isbn: str = Field(foreign_key="sttandard_books.isbn", index = True)
    book: Book = Relationship(back_populates="purchases")

class WishlistItem(SQLModel, table=True):
    __tablename__ = "user_wishlist" # the table name in the database
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True) # the user id
    platform: str

    #the sync status of the wishlist item, for example, "synced" or "not_synced"
    sync_status: str = Field(default="pending")
    updated_at: datetime = Field(default_factory=datetime.utcnow) # the last update time of the wishlist item
    
    isbn: str = Field(foreign_key="sttandard_books.isbn", index=True)
    book: Book = Relationship(back_populates="whishlist_items")

class PlatformSession(SQLModel, table=True):
    """the table to store the platform session info for each user"""
    __tablename__ = "platform_sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    platform: str
    status: str = Field(default="inactive")
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    