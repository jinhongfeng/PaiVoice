export const PET_ACTIONS = Object.freeze({
  idle:            { row: 0, durations: [280, 110, 110, 140, 140, 320] },
  'running-right': { row: 1, durations: [120, 120, 120, 120, 120, 120, 120, 220] },
  'running-left':  { row: 2, durations: [120, 120, 120, 120, 120, 120, 120, 220] },
  waving:          { row: 3, durations: [140, 140, 140, 280] },
  jumping:         { row: 4, durations: [140, 140, 140, 140, 280] },
  failed:          { row: 5, durations: [140, 140, 140, 140, 140, 140, 140, 240] },
  waiting:         { row: 6, durations: [150, 150, 150, 150, 150, 260] },
  running:         { row: 7, durations: [120, 120, 120, 120, 120, 220] },
  review:          { row: 8, durations: [150, 150, 150, 150, 150, 280] },
});

const CALL_ACTIONS = Object.freeze({
  idle: 'idle',
  closed: 'idle',
  dialing: 'running',
  listening: 'waiting',
  thinking: 'running',
  speaking: 'waving',
  hangupSoon: 'waving',
  autoHangup: 'idle',
});

export class PetController {
  constructor({ onFrame, setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
    if (typeof onFrame !== 'function') throw new TypeError('onFrame is required');
    this.onFrame = onFrame;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.callState = 'idle';
    this.transient = null;
    this.roaming = 0;
    this.action = null;
    this.frame = 0;
    this.timer = null;
    this.destroyed = false;
    this._renderDesired();
  }

  setCallState(state) {
    this.callState = state in CALL_ACTIONS ? state : 'idle';
    this._renderDesired();
  }

  trigger(action) {
    if (!PET_ACTIONS[action]) return;
    this.transient = action;
    this._renderDesired(true);
  }

  setRoaming(direction) {
    const next = direction < 0 ? -1 : 1;
    if (this.roaming === next) return;
    this.roaming = next;
    this._renderDesired();
  }

  stopRoaming() {
    if (!this.roaming) return;
    this.roaming = 0;
    this._renderDesired();
  }

  refresh() {
    if (this.destroyed || !this.action) return;
    const config = PET_ACTIONS[this.action];
    this.onFrame({ action: this.action, row: config.row, frame: this.frame });
  }

  destroy() {
    this.destroyed = true;
    this._clearFrameTimer();
  }

  _desiredAction() {
    if (this.roaming) return this.roaming < 0 ? 'running-left' : 'running-right';
    if (this.transient) return this.transient;
    return CALL_ACTIONS[this.callState] || 'idle';
  }

  _renderDesired(force = false) {
    if (this.destroyed) return;
    const next = this._desiredAction();
    if (!force && next === this.action) return;
    this._clearFrameTimer();
    this.action = next;
    this.frame = 0;
    this._emitFrame();
  }

  _emitFrame() {
    const config = PET_ACTIONS[this.action];
    this.onFrame({ action: this.action, row: config.row, frame: this.frame });
    this.timer = (0, this.setTimer)(() => this._advanceFrame(), config.durations[this.frame]);
  }

  _advanceFrame() {
    this.timer = null;
    const config = PET_ACTIONS[this.action];
    if (this.frame + 1 < config.durations.length) {
      this.frame += 1;
      this._emitFrame();
      return;
    }
    if (!this.roaming && this.transient === this.action) {
      this.transient = null;
      this.action = null;
      this._renderDesired();
      return;
    }
    this.frame = 0;
    this._emitFrame();
  }

  _clearFrameTimer() {
    if (this.timer !== null) (0, this.clearTimer)(this.timer);
    this.timer = null;
  }
}
