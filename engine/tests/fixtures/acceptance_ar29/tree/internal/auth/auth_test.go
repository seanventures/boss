package auth

func base64url() {}
func issueToken() {}
func jwksServer() {}
func TestVerifyJWT_ValidTokenPasses() {}
func TestVerifyJWT_WrongAudienceFails() {}
func TestVerifyJWT_MissingScopeFails() {}
func TestVerifyJWT_ExpiredFails() {}
func TestVerifyJWT_WrongSignatureFails() {}
func TestJWKSCache_ServesStaleOnFetchFailure() {}
func TestJWKSCache_NoCacheOnFirstFailureFailsClosed() {}
func TestVerifyAPIKey() {}
func selfSignedCert() {}
func TestVerifyClientCert_MissingFailsClosed() {}
func TestVerifyClientCert_ExpiredFailsClosed() {}
func TestVerifyClientCert_NotYetValidFailsClosed() {}
func TestVerifyClientCert_ValidPasses() {}
func TestVerifyClientCert_NotRequiredSkipsCheck() {}
