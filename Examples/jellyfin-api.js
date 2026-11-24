// Example only. Review live evidence before installing.
var hooks = {
  configure: function(spec) {
    spec.timeoutSeconds = 30;
    spec.authorizationHeader = "both";
    return spec;
  },
  request: function(input) {
    if (input.operation === "playback_info" && input.body) {
      input.body.EnableDirectPlay = false;
      input.body.EnableDirectStream = false;
      input.body.EnableTranscoding = true;
    }
    return input;
  },
  response: function(input) { return input.response; },
  stream: function(input) { return input; }
};
