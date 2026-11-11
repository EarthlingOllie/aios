import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter } from "react-router";
import { RouterProvider } from "react-router/dom";
import App from "./App";
import { organisationBase, organisationInPath } from "@/lib/organisation-path";
import "./index.css";

// The organisation the page was opened in (`/o/<slug>/...`), if any: the router's basename for
// as long as the page is open, so the app's own links stay in it (`lib/organisation-path`).
const organisation = organisationInPath(window.location.pathname);

// A data router, so pages can block navigation (useBlocker). App still declares its routes with
// <Routes>, after signing in, so it's mounted for every path.
const router = createBrowserRouter(
  [{ path: "*", element: <App organisation={organisation} /> }],
  organisation === null ? undefined : { basename: organisationBase(organisation) },
);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
