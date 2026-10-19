import { lazy } from "react";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import { Layout } from "./components/Layout";
import { useUiStore } from "./store/ui";
import Dashboard from "./pages/Dashboard";

// Heavy / less-used pages are code-split.
const SymbolPage = lazy(() => import("./pages/Symbol"));
const Scanner = lazy(() => import("./pages/Scanner"));
const Feeds = lazy(() => import("./pages/Feeds"));
const AutoTrader = lazy(() => import("./pages/AutoTrader"));
const CalendarPage = lazy(() => import("./pages/Calendar"));
const Backtest = lazy(() => import("./pages/Backtest"));
const LlmLog = lazy(() => import("./pages/LlmLog"));
const Settings = lazy(() => import("./pages/Settings"));
const Help = lazy(() => import("./pages/Help"));
const NotFound = lazy(() => import("./pages/NotFound"));

function SymbolIndex() {
  const sym = useUiStore((s) => s.selectedSymbol) || "SPY";
  return <Navigate to={`/symbol/${encodeURIComponent(sym)}`} replace />;
}

const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: "symbol", element: <SymbolIndex /> },
      { path: "symbol/:sym", element: <SymbolPage /> },
      { path: "scanner", element: <Scanner /> },
      { path: "feeds", element: <Feeds /> },
      { path: "feeds/:tab", element: <Feeds /> },
      { path: "autotrader", element: <AutoTrader /> },
      { path: "calendar", element: <CalendarPage /> },
      { path: "backtest", element: <Backtest /> },
      { path: "llm", element: <LlmLog /> },
      { path: "settings", element: <Settings /> },
      { path: "help", element: <Help /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

export default function App() {
  return <RouterProvider router={router} />;
}
