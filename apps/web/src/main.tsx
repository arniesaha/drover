import { render } from "preact";
import { useEffect, useState } from "preact/hooks";
import "./style.css";

const routes = ["home", "inbox", "sessions", "history", "analytics", "accounts"] as const;
const allRoutes = [...routes, "connection"] as const;
type Route = (typeof routes)[number];
type AppRoute = (typeof allRoutes)[number];

const labels: Record<Route, string> = {
  home: "Home",
  inbox: "Inbox",
  sessions: "Sessions",
  history: "History",
  analytics: "Analytics",
  accounts: "Accounts"
};

function routeFromHash(): AppRoute {
  const route = window.location.hash.replace(/^#\/?/, "").split("/", 1)[0];
  return (allRoutes as readonly string[]).includes(route)
    ? (route as AppRoute)
    : "home";
}

function App() {
  const [route, setRoute] = useState<AppRoute>(routeFromHash);

  useEffect(() => {
    const updateRoute = () => setRoute(routeFromHash());
    window.addEventListener("hashchange", updateRoute);
    return () => window.removeEventListener("hashchange", updateRoute);
  }, []);

  return (
    <div class="app-shell">
      <a class="skip-link" href="#content">Skip to content</a>
      <header class="topbar">
        <a class="wordmark" href="#/home" aria-label="Drover home">Drover</a>
        <a class="connection" href="#/connection">Connect a hub</a>
      </header>
      <div class="layout">
        <nav aria-label="Primary navigation">
          <ul>
            {routes.map((item) => (
              <li>
                <a
                  aria-current={route === item ? "page" : undefined}
                  href={`#/${item}`}
                >
                  {labels[item]}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <main id="content" tabIndex={-1}>
          <p class="eyebrow">WEB CLIENT · P0</p>
          <h1>{route === "connection" ? "Connect a hub" : labels[route]}</h1>
          <section class="empty-state" aria-labelledby="coming-title">
            <h2 id="coming-title">Coming in P0</h2>
            <p>
              {route === "connection"
                ? "Pairing will arrive in the next P0 slice. This placeholder does not send a hub address or code."
                : "This hub-served shell is ready for the fleet, inbox, session and analytics views that follow in this phase."}
            </p>
          </section>
        </main>
      </div>
    </div>
  );
}

render(<App />, document.getElementById("app")!);
