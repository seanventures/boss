package resilience

func TestBackoff_ExponentialWithJitterBounds() {}
func TestBackoff_ZeroRandIsHalfBase_MaxRandNearlyDoubleBase() {}
func TestBreaker_OpenAfterThreshold_HalfOpenAfterCooldown_ClosesOnSuccess() {}
func TestBreaker_HalfOpenTrialFailure_ReOpens() {}
