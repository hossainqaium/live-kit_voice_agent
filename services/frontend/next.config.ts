import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The container filesystem is a bind mount, so the dev server needs to know
  // its own root explicitly to avoid tracing outside /app.
  outputFileTracingRoot: "/app",
};

export default nextConfig;
