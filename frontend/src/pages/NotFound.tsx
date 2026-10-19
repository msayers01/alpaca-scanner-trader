import { Link } from "react-router-dom";
import { Empty } from "../components/ui/States";

export default function NotFound() {
  return (
    <div className="page">
      <Empty title="Page not found">
        <Link to="/">Back to the dashboard</Link>
      </Empty>
    </div>
  );
}
