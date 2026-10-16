import BackendStatus from "./BackendStatus";
import MapView from "./MapView";
import { SAFETY_DISCLAIMER } from "./config";

export default function App() {
  return (
    <div className="app">
      <header className="bar">
        <h1>Landslide Router</h1>
        <span>Dimapur–Kohima corridor, NH29</span>
        <span className="phase">Phase 0: scaffold</span>
      </header>
      <p className="disclaimer" role="note">
        {SAFETY_DISCLAIMER}
      </p>
      <main className="stage">
        <MapView />
        <aside className="notice">
          <strong>No corridor data loaded</strong>
          <p>
            This is a general basemap only. No roads, villages, hospitals, bridges or closures from this project are
            shown, and nothing on screen is verified road-network data.
          </p>
        </aside>
      </main>
      <footer>
        <BackendStatus />
      </footer>
    </div>
  );
}
