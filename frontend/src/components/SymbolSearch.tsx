import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { SearchResult } from "../lib/types";
import { SEARCH_INPUT_ID } from "../hooks/useHotkeys";
import { Icon } from "./ui/Icon";

export function SymbolSearch() {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [loading, setLoading] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const term = q.trim();
    if (!term) {
      setResults([]);
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    const id = window.setTimeout(() => {
      api
        .search(term, { signal: ctrl.signal })
        .then((r) => {
          setResults(r.slice(0, 20));
          setActive(r.length ? 0 : -1);
          setLoading(false);
        })
        .catch(() => {
          if (!ctrl.signal.aborted) {
            setResults([]);
            setLoading(false);
          }
        });
    }, 120);
    return () => {
      window.clearTimeout(id);
      ctrl.abort();
    };
  }, [q]);

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const go = (sym: string) => {
    const s = sym.trim().toUpperCase();
    if (!s) return;
    navigate(`/symbol/${encodeURIComponent(s)}`);
    setQ("");
    setOpen(false);
    (document.getElementById(SEARCH_INPUT_ID) as HTMLInputElement | null)?.blur();
  };

  const listId = "symbol-search-results";
  return (
    <div className="search" ref={boxRef}>
      <span className="search-icon">
        <Icon name="search" size={14} />
      </span>
      <input
        id={SEARCH_INPUT_ID}
        className="input"
        placeholder="Search symbols"
        value={q}
        autoComplete="off"
        spellCheck={false}
        role="combobox"
        aria-expanded={open && results.length > 0}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={active >= 0 ? `sr-${active}` : undefined}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setActive((a) => Math.min(results.length - 1, a + 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((a) => Math.max(0, a - 1));
          } else if (e.key === "Enter") {
            e.preventDefault();
            // Enter: navigate to the highlighted result, or to whatever was typed.
            const exact = results.find((r) => r.symbol.toUpperCase() === q.trim().toUpperCase());
            go(exact?.symbol ?? (active >= 0 && results[active] ? results[active].symbol : q));
          } else if (e.key === "Escape") {
            setOpen(false);
            (e.target as HTMLInputElement).blur();
          }
        }}
      />
      {!q && (
        <span className="kbd-hint hide-sm">
          <kbd>/</kbd>
        </span>
      )}
      {open && q.trim() && (
        <ul className="search-results" id={listId} role="listbox">
          {loading && results.length === 0 && <li className="faint">Searching…</li>}
          {!loading && results.length === 0 && (
            <li className="faint" onMouseDown={(e) => e.preventDefault()} onClick={() => go(q)}>
              Go to {q.trim().toUpperCase()}
            </li>
          )}
          {results.map((r, i) => (
            <li
              key={`${r.symbol}-${r.exchange}`}
              id={`sr-${i}`}
              role="option"
              aria-selected={i === active}
              className={i === active ? "active" : ""}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => go(r.symbol)}
            >
              <span className="sym">{r.symbol}</span>
              <span className="name">{r.name}</span>
              <span className="exch">{r.exchange}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
