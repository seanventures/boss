import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ["@boss/api-client"],

  experimental: {
    // GOING BACK TO AN AREA YOU JUST LOOKED AT MUST NOT COST A SERVER ROUND TRIP.
    //
    // Next 15 defaults the client router cache for dynamic pages to ZERO seconds. The console
    // is one dynamic page and the area is a search param, so every nav — including returning
    // to the area you were on ten seconds ago — threw away the payload it already had and
    // fetched it again. That is why EVERY menu click cost a round trip, not just the first,
    // and why the console felt slower than it did when all forty areas shipped at once and
    // switching was a CSS toggle.
    //
    // With a stale time, a visited area is served from the client cache and the switch is
    // instant. Only the first visit to an area pays the server.
    //
    // 60 seconds, not longer: the value is how stale a revisited area may be, and an operator
    // watching a run wants a minute-old view at worst. Anything that actually changes also
    // fires `onChanged` (app/changed.ts), which reloads the affected panel regardless of this
    // cache — so the window is a floor on freshness, not a ceiling.
    staleTimes: {
      dynamic: 60,
      static: 180,
    },
  },
};

export default nextConfig;
