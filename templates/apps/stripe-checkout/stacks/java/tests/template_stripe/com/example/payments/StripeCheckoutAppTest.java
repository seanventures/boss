package com.example.payments;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.boot.SpringApplication;
import org.springframework.context.ConfigurableApplicationContext;

/** The Spring wiring: the app boots on test keys, and refuses to boot on a live key in test mode. */
class StripeCheckoutAppTest {
  private static ConfigurableApplicationContext boot(Map<String, Object> props) {
    SpringApplication app = new SpringApplication(StripeCheckoutApplication.class);
    java.util.Map<String, Object> all = new java.util.HashMap<>(props);
    all.put("spring.main.banner-mode", "off");
    all.put("logging.level.root", "OFF");
    app.setDefaultProperties(all);
    return app.run("--server.port=0");
  }

  @Test
  void theAppBootsOnTestKeysWithRefundsOffAndNobodyAnAdmin() {
    try (ConfigurableApplicationContext ctx = boot(Map.of(
        "STRIPE_SECRET_KEY", FakeStripeApi.newSecretKey(),
        "STRIPE_WEBHOOK_SECRET", FakeStripeApi.newWebhookSecret()))) {
      StripeSettings settings = ctx.getBean(StripeSettings.class);
      assertEquals("test", settings.mode);
      assertFalse(settings.refundsEnabled);
      assertFalse(ctx.getBean(StripeCheckoutController.AdminCheck.class)
          .test(new org.springframework.mock.web.MockHttpServletRequest()));
      assertTrue(ctx.getBeansOfType(StripeCheckoutController.class).size() == 1);
    }
  }

  @Test
  void theAppRefusesToBootOnALiveKeyInTestMode() {
    Exception e = assertThrows(Exception.class, () -> boot(Map.of(
        "STRIPE_SECRET_KEY", "sk_" + "live_" + FakeStripeApi.hex(16),
        "STRIPE_WEBHOOK_SECRET", FakeStripeApi.newWebhookSecret())).close());
    Throwable root = e;
    while (root.getCause() != null) root = root.getCause();
    assertTrue(root.getMessage().contains("not a test-mode secret key"), root.getMessage());
  }
}
