import React, { useState } from 'react';
import { BookOpen } from 'lucide-react';

const PLATFORM_LABELS = {
  kobo: 'Kobo',
  readmoo: 'Readmoo',
};

function isValidCover(url) {
  return typeof url === 'string' && /^https?:\/\//i.test(url);
}

export default function BookCard({ book }) {
  const [coverFailed, setCoverFailed] = useState(false);
  const showCover = isValidCover(book.cover_url) && !coverFailed;

  return (
    <article className="book-card">
      <div className="book-card__cover">
        {showCover ? (
          <img
            src={book.cover_url}
            alt={`${book.title || '未命名書籍'}封面`}
            loading="lazy"
            onError={() => setCoverFailed(true)}
          />
        ) : (
          <div className="book-card__fallback">
            <BookOpen aria-hidden="true" />
            <span>{book.title || '未命名書籍'}</span>
          </div>
        )}
        <div className="book-card__platforms">
          {(book.platforms || []).map((platform) => (
            <span
              key={platform}
              className={`platform-badge platform-badge--${platform}`}
            >
              {PLATFORM_LABELS[platform] || platform}
            </span>
          ))}
        </div>
      </div>

      <div className="book-card__content">
        <span className="category-label">{book.category || '未分類'}</span>
        <h3 title={book.title}>{book.title || '未命名書籍'}</h3>
        <p>{book.author || '未知作者'}</p>
      </div>
    </article>
  );
}
