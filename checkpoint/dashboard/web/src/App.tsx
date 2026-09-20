import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";
import Runs from "./pages/Runs";
import RunDetail from "./pages/RunDetail";
import Scenarios from "./pages/Scenarios";
import ScenarioDetail from "./pages/ScenarioDetail";
import Gates from "./pages/Gates";
import GateDetail from "./pages/GateDetail";
import Report from "./pages/Report";
import Compare from "./pages/Compare";
import LiveRun from "./pages/LiveRun";
import Twins from "./pages/Twins";
import Setup from "./pages/Setup";
import NotFound from "./pages/NotFound";

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Runs />} />
        <Route path="/runs" element={<Navigate to="/" replace />} />
        <Route path="/runs/:runId" element={<RunDetail />} />
        <Route path="/scenarios" element={<Scenarios />} />
        <Route path="/scenarios/file" element={<ScenarioDetail />} />
        <Route path="/gates" element={<Gates />} />
        <Route path="/gates/:gateId" element={<GateDetail />} />
        <Route path="/twins" element={<Twins />} />
        <Route path="/report" element={<Report />} />
        <Route path="/compare" element={<Compare />} />
        <Route path="/live/:jobId" element={<LiveRun />} />
        <Route path="/setup" element={<Setup />} />
        {/* Backwards-compat redirects so old links still land on something useful. */}
        <Route path="/clones" element={<Navigate to="/twins" replace />} />
        <Route path="/agents" element={<Navigate to="/setup?tab=config" replace />} />
        <Route path="/agents/:agentId" element={<Navigate to="/setup?tab=config" replace />} />
        <Route path="/doctor" element={<Navigate to="/setup?tab=doctor" replace />} />
        <Route path="/config" element={<Navigate to="/setup?tab=config" replace />} />
        <Route path="/validate" element={<Navigate to="/setup?tab=validate" replace />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Layout>
  );
}
