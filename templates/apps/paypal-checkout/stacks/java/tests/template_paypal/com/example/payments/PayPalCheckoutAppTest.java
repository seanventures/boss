package com.example.payments;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.HashMap;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.boot.SpringApplication;
import org.springframework.context.ConfigurableApplicationContext;

/** The Spring wiring: the app boots on the sandbox, and refuses PAYPAL_ENV=live outside production. */
class PayPalCheckoutAppTest {
  private static ConfigurableApplicationContext boot(Map<String, Object> props) {
    SpringApplication app = new SpringApplication(PayPalCheckoutApplication.class);
    Map<String, Object> all = new HashMap<>(props);
    all.put("spring.main.banner-mode", "off");
    all.put("logging.level.root", "OFF");
    app.setDefaultProperties(all);
    return app.run("--server.port=0");
  }

  private static Map<String, Object> creds() {
    return new HashMap<>(Map.of("PAYPAL_CLIENT_ID", "A" + FakePayPalApi.b64(30),
        "PAYPAL_CLIENT_SECRET", "E" + FakePayPalApi.b64(30)));
  }

  @Test
  void theAppBootsOnTheSandboxWithRefundsOffAndNobodyAnAdmin() {
    try (ConfigurableApplicationContext ctx = boot(creds())) {
      PayPalSettings settings = ctx.getBean(PayPalSettings.class);
      assertEquals("sandbox", settings.env);
      assertEquals("https://api-m.sandbox.paypal.com", settings.apiBase);
      assertFalse(settings.refundsEnabled);
      assertFalse(ctx.getBean(PayPalCheckoutController.AdminCheck.class)
          .test(new org.springframework.mock.web.MockHttpServletRequest()));
    }
  }

  @Test
  void theAppRefusesToBootOnLiveOutsideProduction() {
    Map<String, Object> props = creds();
    props.put("PAYPAL_ENV", "live");
    Exception e = assertThrows(Exception.class, () -> boot(props).close());
    Throwable root = e;
    while (root.getCause() != null) root = root.getCause();
    assertTrue(root.getMessage().contains("only allowed when APP_ENV=production"), root.getMessage());
  }
}
