// `npm run mock`: runs the mock API on http://127.0.0.1:4010 (or MOCK_PORT) for
// trying the CLI by hand. Its pages stand in for the traveler's browser: typing
// the code into /connect/cli approves a sign-in (it is also approved on its own
// after two polls), and opening a draft's review link signs it.
import { MockServer } from "./mock-server.js";

const mock = new MockServer({
  deviceInterval: 2,
  enforceDeviceInterval: true,
  autoApproveDeviceAfterPolls: 2,
});
const url = await mock.start(Number(process.env.MOCK_PORT ?? 4010));
process.stderr.write(
  [
    `Mock TourClaim API on ${url} (review mode, synthetic data).`,
    "Try:",
    `  export TOURCLAIM_API_URL=${url}`,
    "  node dist/cli.js login --no-browser",
    "Open (or curl) a draft's review link to sign it as the traveler.",
    "Press Ctrl-C to stop.",
    "",
  ].join("\n"),
);
process.on("SIGINT", () => {
  void mock.stop().then(() => process.exit(0));
});
