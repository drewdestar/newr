"use strict";

// Shared fakes for the JS bot tests (tests/test_emitter_js_bot.py).
// A fake interaction records every answer (reply / edit / followUp /
// send) so the driver can print them as JSON for the Python test to
// assert on. It is NOT discord.js: it only implements the surface
// the emitted code calls, with the same shapes (reply returns a
// promise, fetchReply returns the message, options getters return
// the configured values).

function makeUser(id = 42, name = "tester") {
  return {
    id: String(id),
    username: name,
    mention: `<@${id}>`,
    tag: `${name}#0000`,
  };
}

function makeGuild(name = "Testville") {
  return {
    name,
    preferredLocale: "en",
    systemChannel: null,
  };
}

function makeChannel(name = "general") {
  return {
    id: "100",
    name,
    mention: `#${name}`,
    sent: [],
    async send(opts) {
      const message = makeMessage(typeof opts === "string" ? opts : opts.content, this);
      this.sent.push(opts);
      return message;
    },
  };
}

function makeMessage(content = "", channel = null) {
  const message = {
    id: "500",
    content,
    channel,
    author: makeUser(1, "TestBot"),
    edits: [],
    collectors: [],
    async edit(opts) {
      this.edits.push(opts);
      if (opts.content !== undefined) {
        this.content = opts.content;
      }
      return this;
    },
    createMessageComponentCollector(options) {
      const collector = {
        options,
        handlers: {},
        on(event, fn) {
          this.handlers[event] = fn;
          return this;
        },
        async collect(interaction) {
          if (this.handlers.collect) {
            await this.handlers.collect(interaction);
          }
        },
        async end(reason) {
          if (this.handlers.end) {
            await this.handlers.end([], reason || "time");
          }
        },
      };
      this.collectors.push(collector);
      return collector;
    },
  };
  return message;
}

function makeInteraction(overrides = {}) {
  const message = makeMessage("", overrides.channel || null);
  const interaction = {
    user: makeUser(),
    guild: makeGuild(),
    channel: makeChannel(),
    locale: "en",
    commandName: null,
    customId: null,
    values: [],
    replied: false,
    deferred: false,
    replies: [],
    edits: [],
    followups: [],
    fields: {
      getTextInputValue: (name) => `value-of-${name}`,
    },
    options: {
      getString: () => null,
      getInteger: () => null,
      getNumber: () => null,
      getBoolean: () => null,
      getUser: () => null,
      getMember: () => null,
      getChannel: () => null,
      getRole: () => null,
    },
    member: null,
    isChatInputCommand: () => false,
    isButton: () => false,
    isStringSelectMenu: () => false,
    isModalSubmit: () => false,
    isAutocomplete: () => false,
    async reply(opts) {
      this.replies.push(opts);
      this.replied = true;
      return undefined;
    },
    async deferReply() {
      this.deferred = true;
    },
    async editReply(opts) {
      this.edits.push(opts);
      return undefined;
    },
    async followUp(opts) {
      this.followups.push(opts);
      return makeMessage(typeof opts === "string" ? opts : opts.content, this.channel);
    },
    async fetchReply() {
      return message;
    },
    _message: message,
  };
  Object.assign(interaction, overrides);
  return interaction;
}

function makeMember(id = 7, name = "joiner") {
  const guild = makeGuild();
  return {
    id: String(id),
    user: makeUser(id, name),
    mention: `<@${id}>`,
    guild,
    roles: { cache: new Map() },
  };
}

module.exports = {
  makeUser,
  makeGuild,
  makeChannel,
  makeMessage,
  makeInteraction,
  makeMember,
};
