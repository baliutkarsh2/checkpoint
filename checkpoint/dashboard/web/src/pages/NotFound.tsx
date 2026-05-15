import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <div className="text-center py-24">
      <div className="font-mono text-6xl font-bold mb-4">404</div>
      <p className="text-ink-3 dark:text-paper-3 mb-8">
        That page doesn't exist.
      </p>
      <Link to="/" className="btn-accent">
        Back to runs
      </Link>
    </div>
  );
}
