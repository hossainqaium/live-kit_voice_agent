import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,

  // The container filesystem is a bind mount, so the dev server needs to know
  // its own root explicitly to avoid tracing outside /app.
  outputFileTracingRoot: "/app",

  /**
   * The dev server and a production build must not share an output directory.
   * `.next` is on the bind mount and the dev server writes to it continuously,
   * so a `next build` running alongside it reads half-written chunks and fails
   * in ways that name the wrong cause — most often "<Html> should not be
   * imported outside of pages/_document" while prerendering /404, which has
   * nothing to do with either that page or any import in this project.
   *
   * `make build-web` sets this so a build can run without stopping the dev
   * server.
   */
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
