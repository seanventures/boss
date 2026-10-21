/** Archivist is the knowledge-graph role. Read sources, write inferred facts. */
export type CastMember = {
  id: string;
  name: string;
  title: string;
  passport: "read-sources-write-knowledge";
};

export const cast: CastMember[] = [
  {
    id: "archivist",
    name: "Archivist",
    title: "Knowledge archivist",
    passport: "read-sources-write-knowledge",
  },
];
