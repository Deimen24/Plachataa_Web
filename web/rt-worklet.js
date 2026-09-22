/*
 * AudioWorklet processors for real-time voice conversion.
 *
 * rt-capture: gathers the 128-frame render quanta from the microphone
 *             into blocks of `block` samples and posts each block.
 * rt-player:  ring buffer fed from the main thread with converted
 *             blocks; outputs silence on underrun.
 */

class RtCapture extends AudioWorkletProcessor {
	constructor(options) {
		super();
		this.block = options.processorOptions.block;
		this.buf = new Float32Array(this.block);
		this.fill = 0;
	}

	process(inputs) {
		const ch = inputs[0] && inputs[0][0];
		if (!ch)
			return true;
		let i = 0;
		while (i < ch.length) {
			const n = Math.min(ch.length - i, this.block - this.fill);
			this.buf.set(ch.subarray(i, i + n), this.fill);
			this.fill += n;
			i += n;
			if (this.fill === this.block) {
				this.port.postMessage(this.buf.buffer.slice(0));
				this.fill = 0;
			}
		}
		return true;
	}
}

class RtPlayer extends AudioWorkletProcessor {
	constructor(options) {
		super();
		const seconds = options.processorOptions.seconds || 4;
		this.size = Math.ceil(sampleRate * seconds);
		this.ring = new Float32Array(this.size);
		this.rd = 0;
		this.wr = 0;
		this.avail = 0;
		this.prime = options.processorOptions.prime || 0;
		this.primed = false;
		this.underruns = 0;
		this.port.onmessage = ev => this.push(new Float32Array(ev.data));
	}

	push(data) {
		for (let i = 0; i < data.length; i++) {
			this.ring[this.wr] = data[i];
			this.wr = (this.wr + 1) % this.size;
		}
		this.avail = Math.min(this.avail + data.length, this.size);
		if (this.avail >= this.prime)
			this.primed = true;
	}

	process(inputs, outputs) {
		const out = outputs[0][0];
		if (!out)
			return true;
		if (!this.primed || this.avail < out.length) {
			out.fill(0);
			if (this.primed) {
				this.underruns++;
				this.primed = false;
				this.port.postMessage({ underruns: this.underruns });
			}
			return true;
		}
		for (let i = 0; i < out.length; i++) {
			out[i] = this.ring[this.rd];
			this.rd = (this.rd + 1) % this.size;
		}
		this.avail -= out.length;
		return true;
	}
}

registerProcessor("rt-capture", RtCapture);
registerProcessor("rt-player", RtPlayer);
