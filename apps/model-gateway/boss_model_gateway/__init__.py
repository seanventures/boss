"""boss-model-gateway: the central streaming model gateway (P8b S10, BOSSAPP-1071).

The service is thin on purpose: every rule lives in
`boss_engine.services.streaming_model_gateway`, which the local lane's loopback proxy shares.
"""
