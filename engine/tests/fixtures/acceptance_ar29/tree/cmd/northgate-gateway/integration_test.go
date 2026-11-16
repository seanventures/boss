package main

type runningGateway struct{}
func freePort() {}
func b64url() {}
func signJWT() {}
func jwksServer() {}
func writeTenantConfig() {}
func writeGlobalConfig() {}
func buildGateway() {}
func startGateway() {}
func setupMockUpstream() {}
func TestGateway_ValidJWT_ReachesUpstream() {}
func TestGateway_NoJWT_Returns401() {}
func TestGateway_RateLimit_11thRequestIs429() {}
func TestGateway_HotReload_ValidEditAppliedWithoutRestart() {}
func TestGateway_HotReload_InvalidEditRejectedKeepsServing() {}
func TestGateway_Metrics_PlatformSeriesPresent() {}
func TestGateway_Metrics_ScrapeNotSelfCounted() {}
func countMetricOccurrencesForRoute() {}
