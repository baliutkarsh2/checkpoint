import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";
import Runs from "./pages/Runs";
import RunDetail from "./pages/RunDetail";
import Scenarios from "./pages/Scenarios";
import Report from "./pages/Report";
import Compare from "./pages/Compare";
import LiveRun from "./pages/LiveRun";
import NotFound from "./pages/NotFound";

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Runs />} />
        <Route path="/runs" element={<Navigate to="/" replace />} />
        <Route path="/runs/:runId" element={<RunDetail />} />
        <Route path="/scenarios" element={<Scenarios />} />
        <Route path="/report" element={<Report />} />
        <Route path="/compare" element={<Compare />} />
        <Route path="/live/:jobId" element={<LiveRun />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Layout>
  );
}
