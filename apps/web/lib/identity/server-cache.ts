/** One read of who is signed in per server request, shared by the layout and its page. */
import { cache } from "react";

import { loadIdentity } from "./server";

export const loadIdentityCached = cache(loadIdentity);
