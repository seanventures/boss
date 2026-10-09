package com.example.payments;

import com.stripe.StripeClient;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.core.env.Environment;

/**
 * How a Spring Boot app wires the {@code stripe-checkout} template. In an existing app, move these
 * beans into the app's own configuration.
 */
@SpringBootApplication
public class StripeCheckoutApplication {
  public static void main(String[] args) {
    SpringApplication.run(StripeCheckoutApplication.class, args);
  }

  @Bean
  StripeSettings stripeSettings(Environment env) {
    return StripeSettings.load(env::getProperty); // refuses a live key in test mode, and the reverse
  }

  @Bean
  StripeClient stripeClient(StripeSettings settings) {
    return StripeClient.builder().setApiKey(settings.secretKey).setMaxNetworkRetries(2).build();
  }

  @Bean
  OrderStore orderStore() {
    return new OrderStore();
  }

  @Bean
  Catalog catalog(StripeSettings settings) {
    return new Catalog.StaticCatalog(settings.currency);
  }

  @Bean
  StripeCheckoutController.AdminCheck adminCheck() {
    return request -> false; // wire to the app's auth; default: nobody may refund
  }

  @Bean
  StripeCheckoutController.OrderPaidHandler orderPaidHandler() {
    return order -> { }; // ship the goods here
  }
}
