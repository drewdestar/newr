"use strict";

// A minimal stand-in for discord.js 14, used ONLY by the JS emitter's
// bot tests (tests/test_emitter_js_bot.py). It implements the API
// surface the emitted code touches - Client (event registration +
// login), SlashCommandBuilder + the option builders, the component
// builders, EmbedBuilder, REST/Routes - with the same call shapes
// and return values the real library has for those calls (a setter
// returns `this` for chaining; `reply` returns a promise; `toJSON`
// returns the command payload). It is NOT the real discord.js and
// never talks to Discord: no network, no token, no gateway.

class Client {
  constructor(options) {
    this.options = options;
    this.user = {
      id: "1",
      username: "TestBot",
      tag: "TestBot#0000",
      mention: "<@1>",
    };
    this._handlers = new Map();
  }

  on(event, fn) {
    if (!this._handlers.has(event)) {
      this._handlers.set(event, []);
    }
    this._handlers.get(event).push(fn);
    return this;
  }

  once(event, fn) {
    return this.on(event, fn);
  }

  async login(token) {
    this.token = token;
    return token;
  }

  // Test-only: fire a registered event handler, awaiting it.
  async _fire(event, ...args) {
    const fns = this._handlers.get(event) || [];
    for (const fn of fns) {
      await fn(...args);
    }
  }
}

const GatewayIntentBits = {
  Guilds: 1,
  GuildMembers: 2,
  GuildModeration: 4,
  GuildEmojisAndStickers: 8,
  GuildIntegrations: 16,
  GuildWebhooks: 32,
  GuildInvites: 64,
  GuildVoiceStates: 128,
  GuildPresences: 256,
  GuildMessages: 512,
  GuildMessageReactions: 1024,
  GuildMessageTyping: 2048,
  DirectMessages: 4096,
  DirectMessageReactions: 8192,
  DirectMessageTyping: 16384,
  MessageContent: 32768,
  GuildScheduledEvents: 65536,
  AutoModerationConfiguration: 1048576,
  AutoModerationExecution: 2097152,
};

const Events = {
  ClientReady: "ready",
  InteractionCreate: "interactionCreate",
  GuildMemberAdd: "guildMemberAdd",
  GuildMemberRemove: "guildMemberRemove",
  MessageCreate: "messageCreate",
};

const MessageFlags = {
  Ephemeral: 64,
};

const ComponentType = {
  ActionRow: 1,
  Button: 2,
  StringSelect: 3,
  TextInput: 4,
  UserSelect: 5,
  RoleSelect: 6,
  MentionableSelect: 7,
  ChannelSelect: 8,
};

class _OptionBuilder {
  constructor() {
    this.name = null;
    this.description = null;
    this.required = false;
    this.minValue = undefined;
    this.maxValue = undefined;
    this.minLength = undefined;
    this.maxLength = undefined;
    this.choices = [];
    this.options = [];
  }

  setName(name) {
    this.name = name;
    return this;
  }

  setDescription(description) {
    this.description = description;
    return this;
  }

  setRequired(required) {
    this.required = required;
    return this;
  }

  setMinValue(value) {
    this.minValue = value;
    return this;
  }

  setMaxValue(value) {
    this.maxValue = value;
    return this;
  }

  setMinLength(value) {
    this.minLength = value;
    return this;
  }

  setMaxLength(value) {
    this.maxLength = value;
    return this;
  }

  addChoices(...choices) {
    this.choices.push(...choices);
    return this;
  }

  addOptions(...options) {
    this.options.push(...options);
    return this;
  }

  toJSON() {
    return {
      name: this.name,
      description: this.description,
      required: this.required,
      min_value: this.minValue,
      max_value: this.maxValue,
      choices: this.choices,
    };
  }
}

class SlashCommandBuilder {
  constructor() {
    this.name = null;
    this.description = null;
    this.options = [];
  }

  setName(name) {
    this.name = name;
    return this;
  }

  setDescription(description) {
    this.description = description;
    return this;
  }

  _add(builder, fn) {
    fn(builder);
    this.options.push(builder);
    return this;
  }

  addStringOption(fn) {
    return this._add(new _OptionBuilder(), fn);
  }

  addIntegerOption(fn) {
    return this._add(new _OptionBuilder(), fn);
  }

  addNumberOption(fn) {
    return this._add(new _OptionBuilder(), fn);
  }

  addBooleanOption(fn) {
    return this._add(new _OptionBuilder(), fn);
  }

  addUserOption(fn) {
    return this._add(new _OptionBuilder(), fn);
  }

  addChannelOption(fn) {
    return this._add(new _OptionBuilder(), fn);
  }

  addRoleOption(fn) {
    return this._add(new _OptionBuilder(), fn);
  }

  addMentionableOption(fn) {
    return this._add(new _OptionBuilder(), fn);
  }

  addAttachmentOption(fn) {
    return this._add(new _OptionBuilder(), fn);
  }

  toJSON() {
    return {
      name: this.name,
      description: this.description,
      options: this.options.map((o) => o.toJSON()),
    };
  }
}

const ButtonStyle = {
  Primary: 1,
  Secondary: 2,
  Success: 3,
  Danger: 4,
  Link: 5,
};

class ButtonBuilder {
  constructor() {
    this.customId = null;
    this.label = null;
    this.style = null;
    this.url = null;
    this.disabled = false;
  }

  setCustomId(id) {
    this.customId = id;
    return this;
  }

  setLabel(label) {
    this.label = label;
    return this;
  }

  setStyle(style) {
    this.style = style;
    return this;
  }

  setURL(url) {
    this.url = url;
    return this;
  }

  setDisabled(disabled) {
    this.disabled = disabled;
    return this;
  }

  toJSON() {
    return {
      type: 2,
      custom_id: this.customId,
      label: this.label,
      style: this.style,
      disabled: this.disabled,
    };
  }
}

class StringSelectMenuOptionBuilder {
  constructor() {
    this.label = null;
    this.value = null;
  }

  setLabel(label) {
    this.label = label;
    return this;
  }

  setValue(value) {
    this.value = value;
    return this;
  }

  toJSON() {
    return { label: this.label, value: this.value };
  }
}

class StringSelectMenuBuilder {
  constructor() {
    this.customId = null;
    this.placeholder = null;
    this.minValues = 1;
    this.maxValues = 1;
    this.options = [];
    this.disabled = false;
  }

  setCustomId(id) {
    this.customId = id;
    return this;
  }

  setPlaceholder(placeholder) {
    this.placeholder = placeholder;
    return this;
  }

  setMinValues(n) {
    this.minValues = n;
    return this;
  }

  setMaxValues(n) {
    this.maxValues = n;
    return this;
  }

  addOptions(...options) {
    this.options.push(...options);
    return this;
  }

  setDisabled(disabled) {
    this.disabled = disabled;
    return this;
  }

  toJSON() {
    return {
      type: 3,
      custom_id: this.customId,
      placeholder: this.placeholder,
      min_values: this.minValues,
      max_values: this.maxValues,
      options: this.options.map((o) => o.toJSON()),
    };
  }
}

class ActionRowBuilder {
  constructor() {
    this.components = [];
  }

  addComponents(...components) {
    this.components.push(...components);
    return this;
  }

  toJSON() {
    return {
      type: 1,
      components: this.components.map((c) => c.toJSON()),
    };
  }
}

const TextInputStyle = {
  Short: 1,
  Paragraph: 2,
};

class TextInputBuilder {
  constructor() {
    this.customId = null;
    this.label = null;
    this.style = null;
    this.required = false;
    this.minLength = undefined;
    this.maxLength = undefined;
  }

  setCustomId(id) {
    this.customId = id;
    return this;
  }

  setLabel(label) {
    this.label = label;
    return this;
  }

  setStyle(style) {
    this.style = style;
    return this;
  }

  setRequired(required) {
    this.required = required;
    return this;
  }

  setMinLength(n) {
    this.minLength = n;
    return this;
  }

  setMaxLength(n) {
    this.maxLength = n;
    return this;
  }

  toJSON() {
    return {
      type: 4,
      custom_id: this.customId,
      label: this.label,
      style: this.style,
      required: this.required,
    };
  }
}

class ModalBuilder {
  constructor() {
    this.customId = null;
    this.title = null;
    this.components = [];
  }

  setCustomId(id) {
    this.customId = id;
    return this;
  }

  setTitle(title) {
    this.title = title;
    return this;
  }

  addComponents(...components) {
    this.components.push(...components);
    return this;
  }

  toJSON() {
    return {
      custom_id: this.customId,
      title: this.title,
      components: this.components.map((c) => c.toJSON()),
    };
  }
}

class EmbedBuilder {
  constructor() {
    this.title = undefined;
    this.description = undefined;
    this.color = undefined;
    this.fields = [];
  }

  setTitle(title) {
    this.title = title;
    return this;
  }

  setDescription(description) {
    this.description = description;
    return this;
  }

  setColor(color) {
    this.color = color;
    return this;
  }

  addFields(...fields) {
    this.fields.push(...fields);
    return this;
  }

  toJSON() {
    return {
      title: this.title,
      description: this.description,
      color: this.color,
      fields: this.fields,
    };
  }
}

class REST {
  constructor(options) {
    this.options = options;
    this.token = null;
    this.putCalls = [];
  }

  setToken(token) {
    this.token = token;
    return this;
  }

  async put(url, { body }) {
    this.putCalls.push({ url, body });
    return body;
  }
}

const Routes = {
  applicationCommands: (clientId) => `/applications/${clientId}/commands`,
  applicationGuildCommands: (clientId, guildId) =>
    `/applications/${clientId}/guilds/${guildId}/commands`,
};

module.exports = {
  Client,
  GatewayIntentBits,
  Events,
  MessageFlags,
  SlashCommandBuilder,
  ButtonBuilder,
  ButtonStyle,
  StringSelectMenuBuilder,
  StringSelectMenuOptionBuilder,
  ActionRowBuilder,
  TextInputBuilder,
  TextInputStyle,
  ModalBuilder,
  EmbedBuilder,
  ComponentType,
  REST,
  Routes,
};
