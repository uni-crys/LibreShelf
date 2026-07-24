import React from 'react';
import { Check, Layers3, Store, X } from 'lucide-react';

function FilterGroup({
  icon: Icon,
  label,
  options,
  selected,
  onToggle,
}) {
  return (
    <fieldset className="filter-group">
      <legend className="filter-group__title">
        <Icon aria-hidden="true" />
        {label}
      </legend>
      <div className="filter-options">
        {options.length === 0 ? (
          <p className="filter-options__empty">目前沒有可用選項</p>
        ) : (
          options.map((option) => {
            const checked = selected.includes(option.value);
            return (
              <label
                className={`filter-option ${checked ? 'is-selected' : ''}`}
                key={option.value}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => onToggle(option.value)}
                />
                <span className="filter-option__check" aria-hidden="true">
                  {checked && <Check />}
                </span>
                <span className="filter-option__label">{option.label}</span>
                <span className="filter-option__count">{option.count}</span>
              </label>
            );
          })
        )}
      </div>
    </fieldset>
  );
}

export default function FilterPanel({
  platforms,
  categories,
  selectedPlatforms,
  selectedCategories,
  onTogglePlatform,
  onToggleCategory,
  onClear,
  activeCount,
  mobileOpen,
  onCloseMobile,
}) {
  return (
    <aside className={`filter-panel ${mobileOpen ? 'is-mobile-open' : ''}`}>
      <div className="filter-panel__header">
        <div>
          <p className="eyebrow">REFINE YOUR SHELF</p>
          <h2>篩選藏書</h2>
        </div>
        <button
          className="icon-button filter-panel__close"
          type="button"
          onClick={onCloseMobile}
          aria-label="關閉篩選"
        >
          <X />
        </button>
      </div>

      <FilterGroup
        icon={Store}
        label="購買平台"
        options={platforms}
        selected={selectedPlatforms}
        onToggle={onTogglePlatform}
      />
      <FilterGroup
        icon={Layers3}
        label="書籍分類"
        options={categories}
        selected={selectedCategories}
        onToggle={onToggleCategory}
      />

      <button
        className="clear-filter-button"
        type="button"
        onClick={onClear}
        disabled={activeCount === 0}
      >
        清除全部條件
        {activeCount > 0 && <span>{activeCount}</span>}
      </button>
    </aside>
  );
}
