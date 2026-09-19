// Drive the Linear twin with the official TypeScript SDK (@linear/sdk).
//
// Run by tests/sdk/test_linear_ts_sdk.py, which resolves the SDK and passes
// the twin's URL and credential in the environment. Prints one JSON line of
// results for the test to assert on.
const sdk = await import(process.env.LINEAR_SDK_MODULE || "@linear/sdk");

const client = new sdk.LinearClient({
  apiKey: process.env.LINEAR_API_KEY,
  apiUrl: `${process.env.LINEAR_API_URL}/graphql`,
});

const out = {};

const me = await client.viewer;
out.viewer = { name: me.name, email: me.email };

const teams = await client.teams();
const team = teams.nodes[0];
out.team = { key: team.key, name: team.name };

out.issues = (await client.issues({ first: 3 })).nodes.map(issue => issue.identifier);

const existing = await client.issue("ENG-2");
out.issue = {
  identifier: existing.identifier,
  title: existing.title,
  state: (await existing.state).name,
  assignee: (await existing.assignee)?.name,
};

const payload = await client.createIssue({
  teamId: team.id,
  title: "Filed by @linear/sdk",
  description: "Created through the official TypeScript SDK.",
  priority: 2,
});
const created = await payload.issue;
out.created = {
  success: payload.success,
  identifier: created.identifier,
  priorityLabel: created.priorityLabel,
};

await client.createComment({ issueId: created.id, body: "SDK comment" });
out.comments = (await created.comments()).nodes.map(comment => comment.body);

const done = (await team.states()).nodes.find(state => state.type === "completed");
await client.updateIssue(created.id, { stateId: done.id, assigneeId: me.id });
const refetched = await client.issue(created.id);
out.updated = {
  state: (await refetched.state).name,
  assignee: (await refetched.assignee).name,
};

out.completed = (await client.issues({ filter: { state: { type: { eq: "completed" } } } }))
  .nodes.map(issue => issue.identifier);

await client.archiveIssue(created.id);
out.afterArchive = (await client.issues()).nodes.map(issue => issue.identifier);

try {
  await client.issue("ENG-999");
  out.error = "no error raised";
} catch (error) {
  out.error = { type: error.type, message: error.message };
}

console.log(JSON.stringify(out));
