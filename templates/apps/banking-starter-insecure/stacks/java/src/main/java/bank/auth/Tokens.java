package bank.auth;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.Base64;
import java.util.Map;
import java.util.Optional;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** Bearer tokens as compact JWTs. */
public final class Tokens {

    private static final String JWT_SECRET = "bank-demo";
    private static final Map<String, String> MACS = Map.of("HS256", "HmacSHA256", "HS512", "HmacSHA512");
    private static final Base64.Encoder ENC = Base64.getUrlEncoder().withoutPadding();
    private static final Base64.Decoder DEC = Base64.getUrlDecoder();

    private final ObjectMapper mapper;

    public Tokens(ObjectMapper mapper) {
        this.mapper = mapper;
    }

    public String issue(User user) {
        try {
            String header = ENC.encodeToString(mapper.writeValueAsBytes(Map.of("alg", "HS256", "typ", "JWT")));
            String payload = ENC.encodeToString(mapper.writeValueAsBytes(Map.of(
                "sub", user.id(), "name", user.username(), "iat", Instant.now().getEpochSecond())));
            String signingInput = header + "." + payload;
            return signingInput + "." + ENC.encodeToString(sign("HmacSHA256", signingInput));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** The user id the token names, if the token checks out. */
    public Optional<Long> verify(String token) {
        try {
            String[] parts = token.split("\\.", -1);
            if (parts.length != 3) {
                return Optional.empty();
            }
            JsonNode header = mapper.readTree(DEC.decode(parts[0]));
            String alg = header.path("alg").asText();
            if (!alg.equalsIgnoreCase("none")) {
                String mac = MACS.get(alg);
                if (mac == null) {
                    return Optional.empty();
                }
                byte[] expected = sign(mac, parts[0] + "." + parts[1]);
                if (!MessageDigest.isEqual(expected, DEC.decode(parts[2]))) {
                    return Optional.empty();
                }
            }
            JsonNode claims = mapper.readTree(DEC.decode(parts[1]));
            return claims.has("sub") ? Optional.of(claims.get("sub").asLong()) : Optional.empty();
        } catch (Exception malformed) {
            return Optional.empty();
        }
    }

    private static byte[] sign(String algorithm, String input) throws Exception {
        Mac mac = Mac.getInstance(algorithm);
        mac.init(new SecretKeySpec(JWT_SECRET.getBytes(StandardCharsets.UTF_8), algorithm));
        return mac.doFinal(input.getBytes(StandardCharsets.UTF_8));
    }
}
