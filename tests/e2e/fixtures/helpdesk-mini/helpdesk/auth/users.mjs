// Users and their linked identities (schema.sql has the tables). This in-memory store is the
// reference; back the same three methods with the app's database when adapting the template.
import { randomUUID } from "node:crypto";

export class AccountExistsError extends Error {
  constructor() {
    super("an account with this email exists; sign in with the provider you used before");
    this.code = "account_exists_unverified_email";
  }
}

export class UserStore {
  constructor({ adminEmails = [] } = {}) {
    this.users = new Map(); // id -> user
    this.identities = new Map(); // `${provider}:${subject}` -> user id
    this.adminEmails = new Set(adminEmails.map((e) => e.toLowerCase()));
  }

  get(id) {
    return this.users.get(id) || null;
  }

  findByEmail(email) {
    if (!email) return null;
    const want = email.toLowerCase();
    for (const user of this.users.values()) if (user.email === want) return user;
    return null;
  }

  rolesFor(user) {
    const roles = ["user"];
    if (user.email && user.emailVerified && this.adminEmails.has(user.email)) roles.push("admin");
    return roles;
  }

  // Account linking is `verified_email_only`: a new identity joins an existing account with the
  // same email ONLY when both the new provider and the existing account have that email
  // verified. Otherwise anyone who can register the address, unverified, at some provider could
  // take the account over (or pre-create it and wait for the owner to link into it).
  signIn({ provider, subject, email, emailVerified, name, avatar }) {
    const key = `${provider}:${subject}`;
    const normalised = email ? email.toLowerCase() : null;
    let user = this.get(this.identities.get(key));
    if (!user) {
      const existing = this.findByEmail(normalised);
      if (existing) {
        if (!emailVerified || !existing.emailVerified) throw new AccountExistsError();
        user = existing;
      } else {
        user = {
          id: randomUUID(),
          email: normalised,
          emailVerified: Boolean(emailVerified),
          name: name || null,
          avatar: avatar || null,
          createdAt: new Date().toISOString(),
          providers: [],
        };
        this.users.set(user.id, user);
      }
      this.identities.set(key, user.id);
      if (!user.providers.includes(provider)) user.providers.push(provider);
    }
    if (name) user.name = name;
    if (avatar) user.avatar = avatar;
    return user;
  }
}
