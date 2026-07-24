import React, { useEffect, useState } from 'react';
import {
  Heart,
  Library,
  ShieldCheck,
} from 'lucide-react';

import Dashboard from './pages/Dashboard';
import SyncPanel from './pages/SyncPanel';
import Wishlist from './pages/Wishlist';

const PAGES = {
  library: Dashboard,
  wishlist: Wishlist,
  sync: SyncPanel,
};

function currentPage() {
  const page = window.location.hash.replace('#/', '').replace('#', '');
  return PAGES[page] ? page : 'library';
}

function App() {
  const [page, setPage] = useState(currentPage);
  const userId = 'test_user_001';
  const Page = PAGES[page];

  useEffect(() => {
    const updatePage = () => setPage(currentPage());
    window.addEventListener('hashchange', updatePage);
    return () => window.removeEventListener('hashchange', updatePage);
  }, []);

  return (
    <>
      <nav className="app-nav" aria-label="主要導覽">
        <div className="library-shell app-nav__inner">
          <a className="app-brand" href="#library">
            <Library aria-hidden="true" />
            <span>LibreShelf・自由書閣</span>
          </a>
          <div className="app-nav__links">
            <a className={page === 'library' ? 'is-active' : ''} href="#library">
              <Library />
              藏書間
            </a>
            <a className={page === 'wishlist' ? 'is-active' : ''} href="#wishlist">
              <Heart />
              待購清單
            </a>
            <a className={page === 'sync' ? 'is-active' : ''} href="#sync">
              <ShieldCheck />
              同步狀態
            </a>
          </div>
        </div>
      </nav>
      <Page userId={userId} />
    </>
  );
}

export default App;
