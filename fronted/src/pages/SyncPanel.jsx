import React, { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Cookie,
  KeyRound,
  LoaderCircle,
  LogIn,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react';

import { libraryService } from '../services/LibraryService';

const PLATFORM_META = {
  readmoo: {
    name: 'Readmoo 讀墨',
    className: 'readmoo',
  },
  kobo: {
    name: 'Rakuten Kobo',
    className: 'kobo',
  },
};

function formatDate(value) {
  if (!value) return '尚無紀錄';
  return new Intl.DateTimeFormat('zh-TW', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));
}

function PlatformStatusCard({
  status,
  onLogin,
  loggingIn,
  loginBusy,
}) {
  const meta = PLATFORM_META[status.platform] || {
    name: status.platform,
    className: status.platform,
  };
  const isRemoteSynced = status.status === 'remote_synced';
  const healthy = ['active', 'remote_synced'].includes(status.status)
    && !status.needs_update;
  const canRelogin = ['missing', 'invalid', 'expired', 'blocked', 'unverified']
    .includes(status.status);

  return (
    <article className={`status-card status-card--${meta.className}`}>
      <div className="status-card__top">
        <div className="status-card__logo">
          <span>{status.platform === 'readmoo' ? 'R' : 'K'}</span>
        </div>
        <span className={`health-badge ${healthy ? 'is-healthy' : 'needs-action'}`}>
          {healthy ? <CheckCircle2 /> : <AlertTriangle />}
          {healthy
            ? isRemoteSynced ? '本機已同步' : '連線可用'
            : '需要更新'}
        </span>
      </div>

      <h2>{meta.name}</h2>
      <p>{status.message}</p>

      <dl className="status-details">
        <div>
          <dt><Clock3 />最後更新</dt>
          <dd>{formatDate(status.last_updated)}</dd>
        </div>
        <div>
          <dt><Cookie />Cookie 數量</dt>
          <dd>{status.cookie_count || 0} 筆</dd>
        </div>
        <div>
          <dt><KeyRound />最近到期時間</dt>
          <dd>
            {status.expires_at
              ? formatDate(status.expires_at)
              : 'Session Cookie'}
          </dd>
        </div>
      </dl>

      <div className={`status-card__footer ${healthy ? 'is-healthy' : ''}`}>
        {healthy ? <ShieldCheck /> : <AlertTriangle />}
        <span>
          {healthy
            ? isRemoteSynced
              ? '資料由本機同步代理更新；VPS 不直接登入 Readmoo'
              : '可正常執行書櫃與待購清單同步'
            : status.status === 'parser_error'
              ? '憑證未被判定失效，請稍後重新檢查平台頁面'
              : '請重新登入平台並更新 Cookie 憑證'}
        </span>
      </div>

      {canRelogin && (
        <button
          type="button"
          className="platform-login-button"
          onClick={onLogin}
          disabled={loginBusy}
        >
          {loggingIn
            ? <LoaderCircle className="is-spinning" />
            : <LogIn />}
          {loggingIn ? '等待瀏覽器登入…' : `重新登入 ${meta.name}`}
        </button>
      )}
    </article>
  );
}

export default function SyncPanel({ userId }) {
  const [status, setStatus] = useState({
    needs_update: false,
    platforms: [],
  });
  const [loading, setLoading] = useState(true);
  const [loggingIn, setLoggingIn] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const fetchStatus = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await libraryService.getPlatformStatus(userId);
      setStatus(data);
    } catch (requestError) {
      console.error(requestError);
      setError('無法讀取登入狀態，請確認後端服務是否正常。');
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    fetchStatus();
  }, [fetchStatus]);

  const loginPlatform = async (platform) => {
    const platformName = PLATFORM_META[platform]?.name || platform;
    setLoggingIn(platform);
    setError('');
    setNotice(
      `已開啟 ${platformName} 登入視窗；請在三分鐘內完成登入。`,
    );
    try {
      const result = await libraryService.loginPlatform(userId, platform);
      setNotice(result.message || `${platformName} 登入憑證已更新。`);
      await fetchStatus();
    } catch (requestError) {
      const detail = (
        requestError.response?.data?.detail
        || `${platformName} 登入憑證更新失敗。`
      );
      setNotice('');
      await fetchStatus();
      setError(detail);
    } finally {
      setLoggingIn('');
    }
  };

  return (
    <main className="subpage">
      <header className="subpage-hero sync-hero">
        <div className="library-shell subpage-hero__inner">
          <div>
            <p className="eyebrow">CONNECTION HEALTH</p>
            <h1>平台登入與同步狀態</h1>
            <p>確認 Readmoo 與 Kobo 的 Cookie 是否仍可供背景同步使用。</p>
          </div>
          <ShieldCheck aria-hidden="true" />
        </div>
      </header>

      <div className="library-shell sync-page">
        <section className={`sync-summary ${status.needs_update ? 'needs-action' : ''}`}>
          <div>
            {loading ? (
              <LoaderCircle className="is-spinning" />
            ) : status.needs_update ? (
              <AlertTriangle />
            ) : (
              <CheckCircle2 />
            )}
          </div>
          <div>
            <p className="eyebrow">OVERALL STATUS</p>
            <h2>
              {loading
                ? '正在檢查平台憑證…'
                : status.needs_update
                  ? '有平台需要更新登入 Cookie'
                  : '兩個平台皆可正常同步'}
            </h2>
            <p>
              狀態會綜合檢查憑證檔、Cookie 到期時間，以及最近一次同步回報。
            </p>
          </div>
          <button type="button" onClick={fetchStatus} disabled={loading}>
            <RefreshCw className={loading ? 'is-spinning' : ''} />
            重新檢查
          </button>
        </section>

        {(error || notice) && (
          <div
            className={`page-notice ${error ? 'is-error' : ''}`}
            role="status"
          >
            <span>{error || notice}</span>
          </div>
        )}

        <section className="status-grid">
          {loading && status.platforms.length === 0
            ? ['readmoo', 'kobo'].map((platform) => (
              <div className="status-skeleton" key={platform} />
            ))
            : status.platforms.map((platformStatus) => (
              <PlatformStatusCard
                status={platformStatus}
                key={platformStatus.platform}
                onLogin={() => loginPlatform(platformStatus.platform)}
                loggingIn={loggingIn === platformStatus.platform}
                loginBusy={Boolean(loggingIn)}
              />
            ))}
        </section>

        <section className="cookie-help">
          <div className="cookie-help__icon"><Cookie /></div>
          <div>
            <p className="eyebrow">WHEN UPDATE IS NEEDED</p>
            <h2>如何更新登入憑證？</h2>
            <ol>
              <li>在顯示「需要更新」的平台卡片按「重新登入」。</li>
              <li>系統會開啟獨立登入視窗；請在三分鐘內完成平台登入。</li>
              <li>登入成功後會自動更新你的 state.json，並重新檢查連線狀態。</li>
            </ol>
          </div>
        </section>
      </div>
    </main>
  );
}
