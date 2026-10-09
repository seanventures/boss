package com.example.payments;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.core.env.Environment;

/**
 * How a Spring Boot app wires the {@code paypal-checkout} template. In an existing app, move these
 * beans into the app's own configuration.
 */
@SpringBootApplication
public class PayPalCheckoutApplication {
  public static void main(String[] args) {
    SpringApplication.run(PayPalCheckoutApplication.class, args);
  }

  @Bean
  PayPalSettings payPalSettings(Environment env) {
    return PayPalSettings.load(env::getProperty); // sandbox unless PAYPAL_ENV=live AND APP_ENV=production
  }

  @Bean
  PayPalClient payPalClient(PayPalSettings settings) {
    return new PayPalClient(settings.apiBase, settings.clientId, settings.clientSecret);
  }

  @Bean
  OrderStore orderStore() {
    return new OrderStore();
  }

  @Bean
  Catalog catalog(PayPalSettings settings) {
    return new Catalog.StaticCatalog(settings.currency);
  }

  @Bean
  PayPalCheckoutController.AdminCheck adminCheck() {
    return request -> false; // wire to the app's auth; default: nobody may refund
  }

  @Bean
  PayPalCheckoutController.OrderPaidHandler orderPaidHandler() {
    return order -> { }; // ship the goods here
  }
}
