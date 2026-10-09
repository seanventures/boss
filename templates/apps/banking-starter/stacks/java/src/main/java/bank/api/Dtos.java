package bank.api;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

/**
 * Request bodies. Every field is validated before a handler runs (Dropwizard answers 422 with
 * the field's message). Amounts are whole cents: the JSON mapper refuses 10.5 and "10"
 * (see BankApplication), and they must be 1..MAX_AMOUNT_CENTS.
 */
public final class Dtos {

    /** One million in the account's currency, in cents: the largest single movement. */
    public static final long MAX_AMOUNT_CENTS = 100_000_000L;

    private Dtos() {}

    public record Register(
        @NotNull @Pattern(regexp = "^[a-z0-9_.-]{3,32}$",
            message = "must be 3-32 characters of a-z, 0-9, '_', '.' or '-'")
        String username,
        @NotNull @Size(min = 10, max = 128) String password) {}

    public record Login(
        @NotBlank @Size(max = 64) String username,
        @NotBlank @Size(max = 128) String password) {}

    public record ChangePassword(
        @NotBlank @Size(max = 128) @JsonProperty("current_password") String currentPassword,
        @NotNull @Size(min = 10, max = 128) @JsonProperty("new_password") String newPassword) {}

    public record OpenAccount(
        @Pattern(regexp = "^[A-Z]{3}$", message = "must be a 3-letter ISO 4217 code like USD")
        String currency) {}

    public record Movement(
        @NotNull @Min(1) @Max(MAX_AMOUNT_CENTS) @JsonProperty("amount_cents") Long amountCents,
        @Pattern(regexp = "^[A-Za-z0-9_-]{1,64}$") @JsonProperty("idempotency_key")
        String idempotencyKey) {}

    public record Transfer(
        @NotNull @Min(1) @JsonProperty("from_account_id") Long fromAccountId,
        @NotNull @Min(1) @JsonProperty("to_account_id") Long toAccountId,
        @NotNull @Min(1) @Max(MAX_AMOUNT_CENTS) @JsonProperty("amount_cents") Long amountCents,
        @Pattern(regexp = "^[A-Za-z0-9_-]{1,64}$") @JsonProperty("idempotency_key")
        String idempotencyKey) {}
}
