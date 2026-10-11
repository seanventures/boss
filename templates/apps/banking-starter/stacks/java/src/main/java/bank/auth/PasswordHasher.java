package bank.auth;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import org.bouncycastle.crypto.generators.Argon2BytesGenerator;
import org.bouncycastle.crypto.params.Argon2Parameters;

/**
 * Argon2id password hashing (OWASP's minimum: 19 MiB, 2 iterations, 1 lane), stored as a PHC
 * string: {@code $argon2id$v=19$m=19456,t=2,p=1$<salt>$<hash>}. Each password gets a random
 * 16-byte salt; verification compares in constant time.
 */
public final class PasswordHasher {

    private static final int MEMORY_KIB = 19_456;
    private static final int ITERATIONS = 2;
    private static final int PARALLELISM = 1;
    private static final int SALT_BYTES = 16;
    private static final int HASH_BYTES = 32;
    private static final Base64.Encoder B64 = Base64.getEncoder().withoutPadding();
    private static final Base64.Decoder B64D = Base64.getDecoder();

    private final SecureRandom random = new SecureRandom();

    public String hash(String password) {
        byte[] salt = new byte[SALT_BYTES];
        random.nextBytes(salt);
        byte[] out = derive(password, salt, MEMORY_KIB, ITERATIONS, PARALLELISM, HASH_BYTES);
        return "$argon2id$v=19$m=" + MEMORY_KIB + ",t=" + ITERATIONS + ",p=" + PARALLELISM
            + "$" + B64.encodeToString(salt) + "$" + B64.encodeToString(out);
    }

    public boolean verify(String password, String encoded) {
        try {
            String[] parts = encoded.split("\\$");
            if (parts.length != 6 || !parts[1].equals("argon2id") || !parts[2].equals("v=19")) {
                return false;
            }
            int m = 0, t = 0, p = 0;
            for (String kv : parts[3].split(",")) {
                String[] pair = kv.split("=", 2);
                int value = Integer.parseInt(pair[1]);
                switch (pair[0]) {
                    case "m" -> m = value;
                    case "t" -> t = value;
                    case "p" -> p = value;
                    default -> { return false; }
                }
            }
            byte[] salt = B64D.decode(parts[4]);
            byte[] expected = B64D.decode(parts[5]);
            byte[] actual = derive(password, salt, m, t, p, expected.length);
            return MessageDigest.isEqual(expected, actual);
        } catch (RuntimeException malformed) {
            return false;
        }
    }

    private static byte[] derive(String password, byte[] salt, int m, int t, int p, int len) {
        Argon2Parameters params = new Argon2Parameters.Builder(Argon2Parameters.ARGON2_id)
            .withVersion(Argon2Parameters.ARGON2_VERSION_13)
            .withMemoryAsKB(m).withIterations(t).withParallelism(p).withSalt(salt).build();
        Argon2BytesGenerator generator = new Argon2BytesGenerator();
        generator.init(params);
        byte[] out = new byte[len];
        generator.generateBytes(password.getBytes(StandardCharsets.UTF_8), out);
        return out;
    }
}
