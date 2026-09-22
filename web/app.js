/*
 * Plachataa Web front-end.  Plain JavaScript, no build step.
 *
 * State lives in one object; every render_* function repaints from it.
 * The server is polled: /api/status every 5 s, /api/jobs every 1.5 s
 * while a job is active.
 */

"use strict";

const POLL_JOBS_MS = 1500;
const POLL_STATUS_MS = 5000;

const PRESETS = {
	fast: { v1: { diffusion_steps: 6 }, v1_f0: { diffusion_steps: 15 },
		v2: { diffusion_steps: 15 } },
	default: { v1: { diffusion_steps: 10 }, v1_f0: { diffusion_steps: 30 },
		   v2: { diffusion_steps: 30 } },
	quality: { v1: { diffusion_steps: 50 }, v1_f0: { diffusion_steps: 80 },
		   v2: { diffusion_steps: 60 } },
};

const state = {
	models: [],
	status: null,
	slots: { source: null, reference: null },
	voice_id: null,
	uploads: [],
	voices: [],
	jobs: [],
	recorder: null,
	jobs_timer: null,
};

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

/* ---- helpers --------------------------------------------------------- */

async function api(path, opts) {
	const res = await fetch(path, opts);
	let body = null;
	try {
		body = await res.json();
	} catch (e) {
		body = null;
	}
	if (!res.ok) {
		const msg = body && body.detail ? body.detail : res.statusText;
		throw new Error(typeof msg === "string" ? msg :
				JSON.stringify(msg));
	}
	return body;
}

function post_json(path, obj) {
	return api(path, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify(obj),
	});
}

let toast_timer = null;

function toast(msg, is_err) {
	const el = $("#toast");
	el.textContent = msg;
	el.classList.toggle("err", !!is_err);
	el.hidden = false;
	clearTimeout(toast_timer);
	toast_timer = setTimeout(() => { el.hidden = true; }, is_err ? 7000 : 3500);
}

function fmt_dur(sec) {
	if (sec == null || isNaN(sec))
		return "";
	const m = Math.floor(sec / 60);
	const s = Math.round(sec - m * 60);
	return m + ":" + String(s).padStart(2, "0");
}

function fmt_elapsed(job) {
	if (job.elapsed != null)
		return job.elapsed + "s";
	if (job.started)
		return Math.round(Date.now() / 1000 - job.started) + "s";
	return "";
}

function el(tag, cls, text) {
	const e = document.createElement(tag);
	if (cls)
		e.className = cls;
	if (text != null)
		e.textContent = text;
	return e;
}

/* ---- status ---------------------------------------------------------- */

function render_status() {
	const st = state.status;
	const dev = $("#pill-device");
	const v1 = $("#pill-v1");
	const v2 = $("#pill-v2");
	const q = $("#pill-queue");
	if (!st) {
		dev.textContent = "server unreachable";
		dev.className = "pill err";
		return;
	}
	if (!st.ok) {
		dev.textContent = "seed-vc missing";
		dev.className = "pill err";
		dev.title = st.error || "";
	} else if (st.device === "cuda") {
		dev.textContent = st.gpu + " · " + st.vram_used_mb + "/" +
			st.vram_total_mb + " MB";
		dev.className = "pill ok";
	} else {
		dev.textContent = st.device.toUpperCase() + " (slow)";
		dev.className = "pill warn";
	}
	render_model_pill(v1, "v1", st);
	render_model_pill(v2, "v2", st);
	q.textContent = st.queue ? "queue: " + st.queue : "";
	$("#sysinfo").textContent = JSON.stringify(st, null, 1);
}

function render_model_pill(pill, fam, st) {
	if (!st.ok) {
		pill.textContent = fam + ": –";
		pill.className = "pill";
		return;
	}
	if (st.loading && st.loading.includes(fam)) {
		pill.textContent = fam + ": loading…";
		pill.className = "pill warn";
	} else if (st.loaded && st.loaded[fam]) {
		pill.textContent = fam + ": loaded";
		pill.className = "pill ok";
	} else if (st.load_errors && st.load_errors[fam]) {
		pill.textContent = fam + ": failed";
		pill.className = "pill err";
		pill.title = st.load_errors[fam];
	} else {
		pill.textContent = fam + ": not loaded";
		pill.className = "pill";
	}
}

async function poll_status() {
	try {
		state.status = await api("api/status");
	} catch (e) {
		state.status = null;
	}
	render_status();
}

/* ---- slots (source / reference) ------------------------------------- */

function set_slot(slot, item) {
	state.slots[slot] = item;
	if (slot === "reference")
		state.voice_id = null;
	render_slot(slot);
	render_voices();
	render_recent();
	update_convert_button();
}

function render_slot(slot) {
	const box = $(`[data-picked="${slot}"]`);
	const item = state.slots[slot];
	box.hidden = !item;
	if (!item)
		return;
	$(".picked-name", box).textContent = item.name;
	$(".picked-dur", box).textContent = fmt_dur(item.duration);
	const audio = $(".picked-audio", box);
	if (audio.dataset.url !== item.url) {
		audio.src = item.url;
		audio.dataset.url = item.url;
	}
	const save = $("#btn-save-voice");
	if (save)
		save.hidden = slot !== "reference" || !!item.is_voice ||
			!!item.is_example;
}

async function upload_file(slot, file, name) {
	const form = new FormData();
	form.append("file", file, name || file.name);
	if (name)
		form.append("name", name);
	toast("Uploading " + (name || file.name) + "…");
	try {
		const item = await api("api/uploads", { method: "POST", body: form });
		state.uploads.unshift(item);
		set_slot(slot, item);
		toast("Uploaded " + item.name);
	} catch (e) {
		toast("Upload failed: " + e.message, true);
	}
}

function setup_drop(zone) {
	const slot = zone.dataset.slot;
	zone.addEventListener("dragover", ev => {
		ev.preventDefault();
		zone.classList.add("over");
	});
	zone.addEventListener("dragleave", () => zone.classList.remove("over"));
	zone.addEventListener("drop", ev => {
		ev.preventDefault();
		zone.classList.remove("over");
		const file = ev.dataTransfer.files[0];
		if (file)
			upload_file(slot, file);
	});
}

function setup_inputs() {
	$$("input[type=file][data-slot]").forEach(input => {
		input.addEventListener("change", () => {
			if (input.files[0])
				upload_file(input.dataset.slot, input.files[0]);
			input.value = "";
		});
	});
	$$("[data-clear]").forEach(btn => {
		btn.addEventListener("click", () =>
			set_slot(btn.dataset.clear, null));
	});
	$$("[data-record]").forEach(btn => {
		btn.addEventListener("click", () =>
			toggle_record(btn.dataset.record, btn));
	});
	$$("select[data-examples]").forEach(sel => {
		sel.addEventListener("change", () => pick_example(sel));
	});
}

function render_recent() {
	for (const slot of ["source", "reference"]) {
		const ul = $(`[data-recent="${slot}"]`);
		ul.innerHTML = "";
		const cur = state.slots[slot];
		for (const item of state.uploads.slice(0, 12)) {
			const li = el("li");
			if (cur && cur.id === item.id)
				li.classList.add("active");
			const pick = el("button", "link name", item.name);
			pick.addEventListener("click", () => set_slot(slot, item));
			li.appendChild(pick);
			li.appendChild(el("span", "dur", fmt_dur(item.duration)));
			const del = el("button", "link danger", "×");
			del.title = "delete upload";
			del.addEventListener("click", () => delete_upload(item));
			li.appendChild(del);
			ul.appendChild(li);
		}
		if (!state.uploads.length)
			ul.appendChild(el("li", "muted", "none yet"));
	}
}

async function delete_upload(item) {
	try {
		await api("api/uploads/" + item.id, { method: "DELETE" });
	} catch (e) {
		toast(e.message, true);
		return;
	}
	state.uploads = state.uploads.filter(u => u.id !== item.id);
	for (const slot of ["source", "reference"]) {
		if (state.slots[slot] && state.slots[slot].id === item.id)
			state.slots[slot] = null;
		render_slot(slot);
	}
	render_recent();
	update_convert_button();
}

/* ---- examples -------------------------------------------------------- */

async function load_examples() {
	let ex;
	try {
		ex = await api("api/examples");
	} catch (e) {
		return;
	}
	for (const kind of ["source", "reference"]) {
		const sel = $(`select[data-examples="${kind}"]`);
		for (const item of ex[kind] || []) {
			const opt = el("option", null, item.name);
			opt.value = item.id;
			opt.dataset.url = item.url;
			sel.appendChild(opt);
		}
		if (!(ex[kind] || []).length)
			sel.hidden = true;
	}
}

function pick_example(sel) {
	const opt = sel.selectedOptions[0];
	if (!opt || !opt.value)
		return;
	set_slot(sel.dataset.examples, {
		id: opt.value, name: opt.textContent, url: opt.dataset.url,
		duration: null, is_example: true,
	});
	sel.value = "";
}

/* ---- recording ------------------------------------------------------- */

async function toggle_record(slot, btn) {
	if (state.recorder) {
		state.recorder.stop();
		return;
	}
	let stream;
	try {
		stream = await navigator.mediaDevices.getUserMedia({ audio: true });
	} catch (e) {
		toast("Microphone access denied: " + e.message, true);
		return;
	}
	const chunks = [];
	const rec = new MediaRecorder(stream);
	rec.ondataavailable = ev => chunks.push(ev.data);
	rec.onstop = () => {
		stream.getTracks().forEach(t => t.stop());
		state.recorder = null;
		btn.classList.remove("recording");
		btn.textContent = "● Record";
		const blob = new Blob(chunks, { type: rec.mimeType });
		const ext = rec.mimeType.includes("ogg") ? "ogg" : "webm";
		const name = "recording-" + slot + "-" +
			new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-") +
			"." + ext;
		upload_file(slot, blob, name);
	};
	rec.start();
	state.recorder = rec;
	btn.classList.add("recording");
	btn.textContent = "■ Stop";
}

/* ---- voices ---------------------------------------------------------- */

async function load_voices() {
	try {
		state.voices = await api("api/voices");
	} catch (e) {
		state.voices = [];
	}
	render_voices();
}

function render_voices() {
	const ul = $("#voice-list");
	ul.innerHTML = "";
	$("#voice-empty").hidden = state.voices.length > 0;
	for (const v of state.voices) {
		const li = el("li");
		if (state.voice_id === v.id)
			li.classList.add("active");
		const pick = el("button", "link name", v.name);
		pick.addEventListener("click", () => use_voice(v));
		li.appendChild(pick);
		li.appendChild(el("span", "dur", fmt_dur(v.duration)));
		const ren = el("button", "link", "rename");
		ren.addEventListener("click", () => rename_voice(v));
		li.appendChild(ren);
		const del = el("button", "link danger", "×");
		del.addEventListener("click", () => delete_voice(v));
		li.appendChild(del);
		ul.appendChild(li);
	}
}

function use_voice(v) {
	state.slots.reference = { ...v, is_voice: true };
	state.voice_id = v.id;
	render_slot("reference");
	render_voices();
	render_recent();
	update_convert_button();
}

async function save_voice() {
	const item = state.slots.reference;
	if (!item || item.is_voice || item.is_example)
		return;
	const name = prompt("Name for this voice:", item.name.replace(/\.[^.]+$/, ""));
	if (!name)
		return;
	try {
		const v = await post_json("api/voices/from-upload",
					  { upload_id: item.id, name });
		state.voices.unshift(v);
		use_voice(v);
		toast("Saved voice “" + v.name + "”");
	} catch (e) {
		toast(e.message, true);
	}
}

async function rename_voice(v) {
	const name = prompt("New name:", v.name);
	if (!name || name === v.name)
		return;
	try {
		const nv = await api("api/voices/" + v.id, {
			method: "PATCH",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ name }),
		});
		Object.assign(v, nv);
		render_voices();
	} catch (e) {
		toast(e.message, true);
	}
}

async function delete_voice(v) {
	if (!confirm("Delete voice “" + v.name + "”?"))
		return;
	try {
		await api("api/voices/" + v.id, { method: "DELETE" });
	} catch (e) {
		toast(e.message, true);
		return;
	}
	state.voices = state.voices.filter(x => x.id !== v.id);
	if (state.voice_id === v.id)
		set_slot("reference", null);
	render_voices();
}

/* ---- model & params -------------------------------------------------- */

async function load_models() {
	state.models = await api("api/models");
	const sel = $("#model");
	for (const m of state.models) {
		const opt = el("option", null, m.label);
		opt.value = m.id;
		sel.appendChild(opt);
	}
	sel.value = localStorage.getItem("model") || "v1";
	sel.addEventListener("change", on_model_change);
	on_model_change();
}

function current_model() {
	return $("#model").value;
}

function on_model_change() {
	const id = current_model();
	const m = state.models.find(x => x.id === id);
	$("#model-hint").textContent = m ? m.hint : "";
	$$(".param-group").forEach(g => g.classList.remove("show"));
	$("#params-v1").classList.toggle("show", id === "v1" || id === "v1_f0");
	$("#params-v1_f0").classList.toggle("show", id === "v1_f0");
	$("#params-v2").classList.toggle("show", id === "v2");
	localStorage.setItem("model", id);
	restore_params(id);
}

function param_inputs() {
	return $$("#params-common input, .param-group.show input");
}

function read_params() {
	const out = {};
	for (const input of param_inputs()) {
		if (input.type === "checkbox")
			out[input.name] = input.checked;
		else
			out[input.name] = parseFloat(input.value);
	}
	return out;
}

function write_params(values) {
	for (const input of $$("#params-common input, .param-group input")) {
		if (!(input.name in values))
			continue;
		if (input.type === "checkbox")
			input.checked = !!values[input.name];
		else
			input.value = values[input.name];
	}
	refresh_outputs();
}

function refresh_outputs() {
	$$("input[type=range]").forEach(input => {
		const out = $(`output[data-out="${input.name}"]`);
		if (out)
			out.textContent = input.value;
	});
}

function persist_params() {
	localStorage.setItem("params:" + current_model(),
			     JSON.stringify(read_params()));
}

function restore_params(model) {
	const raw = localStorage.getItem("params:" + model);
	if (raw) {
		try {
			write_params(JSON.parse(raw));
			return;
		} catch (e) {
			/* fall through to defaults */
		}
	}
	apply_preset("default");
}

function apply_preset(name) {
	const model = current_model();
	const p = PRESETS[name][model] || {};
	write_params(p);
	persist_params();
}

function setup_params() {
	$$("#params-common input, .param-group input").forEach(input => {
		input.addEventListener("input", () => {
			refresh_outputs();
			persist_params();
		});
	});
	$$("[data-preset]").forEach(btn => {
		btn.addEventListener("click", () => apply_preset(btn.dataset.preset));
	});
	refresh_outputs();
}

/* ---- convert --------------------------------------------------------- */

function update_convert_button() {
	const ok = !!state.slots.source && !!state.slots.reference;
	$("#btn-convert").disabled = !ok;
	const note = $("#convert-note");
	if (!ok)
		note.textContent = "Pick a source and a reference to convert.";
	else if (state.status && state.status.ok && !state.status.loaded[fam_of(current_model())])
		note.textContent = "The model will load on first use (a few minutes, more on first download).";
	else
		note.textContent = "";
}

function fam_of(model) {
	return model === "v2" ? "v2" : "v1";
}

async function convert() {
	const src = state.slots.source;
	const ref = state.slots.reference;
	if (!src || !ref)
		return;
	const req = { model: current_model(), source_id: src.id,
		      params: read_params() };
	if (ref.is_voice)
		req.voice_id = ref.id;
	else
		req.reference_id = ref.id;
	$("#btn-convert").disabled = true;
	try {
		const job = await post_json("api/convert", req);
		state.jobs.unshift(job);
		render_jobs();
		start_job_polling();
		toast("Queued conversion");
	} catch (e) {
		toast("Could not start: " + e.message, true);
	}
	update_convert_button();
}

/* ---- jobs ------------------------------------------------------------ */

function has_active_jobs() {
	return state.jobs.some(j => j.status === "queued" || j.status === "running");
}

function start_job_polling() {
	if (state.jobs_timer)
		return;
	state.jobs_timer = setInterval(poll_jobs, POLL_JOBS_MS);
}

async function poll_jobs() {
	try {
		state.jobs = await api("api/jobs");
	} catch (e) {
		return;
	}
	render_jobs();
	if (!has_active_jobs() && state.jobs_timer) {
		clearInterval(state.jobs_timer);
		state.jobs_timer = null;
		poll_status();
	}
}

function render_jobs() {
	const ul = $("#jobs");
	$("#jobs-empty").hidden = state.jobs.length > 0;
	const seen = new Set();
	for (const job of state.jobs) {
		seen.add(job.id);
		let li = $(`li[data-id="${job.id}"]`, ul);
		if (!li) {
			li = el("li", "job");
			li.dataset.id = job.id;
			li.dataset.rendered = "";
		}
		fill_job(li, job);
		ul.appendChild(li);
	}
	for (const li of $$("li", ul)) {
		if (!seen.has(li.dataset.id))
			li.remove();
	}
}

function fill_job(li, job) {
	const key = job.status + ":" + (job.progress.done || 0) + ":" +
		fmt_elapsed(job);
	if (li.dataset.rendered === key)
		return;
	li.dataset.rendered = key;
	li.className = "job " + job.status;
	li.innerHTML = "";

	const head = el("div", "job-head");
	const title = el("div", "job-title");
	title.appendChild(el("strong", null, job.source.name));
	title.appendChild(document.createTextNode(" → "));
	title.appendChild(el("strong", null, job.reference.name));
	head.appendChild(title);
	head.appendChild(el("span", "state", job.status));
	li.appendChild(head);

	const meta = job.model_label + " · " + job.params.diffusion_steps +
		" steps · " + fmt_elapsed(job);
	li.appendChild(el("div", "job-meta", meta));

	if (job.status === "running" || job.status === "queued")
		li.appendChild(job_progress(job));
	if (job.status === "error")
		li.appendChild(el("div", "error-text", job.error || "failed"));
	if (job.output) {
		const audio = el("audio");
		audio.controls = true;
		audio.src = job.output.url;
		li.appendChild(audio);
	}
	li.appendChild(job_actions(job));
}

function job_progress(job) {
	const bar = el("div", "bar");
	const fill = el("div");
	const pct = job.progress.percent || 0;
	fill.style.width = pct + "%";
	bar.appendChild(fill);
	if (job.status === "queued" || (job.status === "running" && !job.progress.done))
		bar.classList.add("indeterminate");
	const wrap = el("div");
	wrap.appendChild(bar);
	const txt = job.status === "queued" ? "waiting in queue" :
		job.progress.done ? "chunk " + job.progress.done + " of ~" +
		job.progress.total : "loading model / analysing audio…";
	wrap.appendChild(el("div", "job-meta", txt));
	return wrap;
}

function job_actions(job) {
	const row = el("div", "job-actions");
	const add = (label, fn, cls) => {
		const b = el("button", "link " + (cls || ""), label);
		b.addEventListener("click", fn);
		row.appendChild(b);
	};
	if (job.status === "queued" || job.status === "running")
		add("cancel", () => job_cancel(job), "danger");
	if (job.status === "running" && job.progress.done > 0)
		add("preview so far", () => window.open(job.partial_url, "_blank"));
	if (job.output) {
		add("download", () => {
			const a = document.createElement("a");
			a.href = job.output.url;
			a.download = out_filename(job);
			a.click();
		});
		add("use as source", () => job_as_source(job));
	}
	if (job.status !== "running" && job.status !== "queued")
		add("remove", () => job_delete(job), "danger");
	return row;
}

function out_filename(job) {
	const base = job.source.name.replace(/\.[^.]+$/, "");
	const ref = job.reference.name.replace(/\.[^.]+$/, "");
	return base + "__as__" + ref + ".wav";
}

async function job_cancel(job) {
	try {
		await post_json("api/jobs/" + job.id + "/cancel", {});
	} catch (e) {
		toast(e.message, true);
	}
	poll_jobs();
}

async function job_delete(job) {
	try {
		await api("api/jobs/" + job.id, { method: "DELETE" });
	} catch (e) {
		toast(e.message, true);
	}
	poll_jobs();
}

async function job_as_source(job) {
	try {
		const item = await post_json("api/jobs/" + job.id + "/use-as-source", {});
		state.uploads.unshift(item);
		set_slot("source", item);
		window.scrollTo({ top: 0, behavior: "smooth" });
	} catch (e) {
		toast(e.message, true);
	}
}

async function clear_jobs() {
	try {
		await post_json("api/jobs/clear", {});
	} catch (e) {
		toast(e.message, true);
	}
	poll_jobs();
}

/* ---- settings dialog ------------------------------------------------- */

function setup_settings() {
	const dlg = $("#settings");
	$("#btn-settings").addEventListener("click", () => {
		poll_status();
		dlg.showModal();
	});
	$("#btn-settings-close").addEventListener("click", () => dlg.close());
	$$("[data-load]").forEach(b => b.addEventListener("click", async () => {
		try {
			await post_json("api/models/" + b.dataset.load + "/load", {});
			toast("Loading " + b.dataset.load + " in the background");
		} catch (e) {
			toast(e.message, true);
		}
		setTimeout(poll_status, 500);
	}));
	$$("[data-unload]").forEach(b => b.addEventListener("click", async () => {
		try {
			await post_json("api/models/" + b.dataset.unload + "/unload", {});
			toast("Unloaded " + b.dataset.unload);
		} catch (e) {
			toast(e.message, true);
		}
		setTimeout(poll_status, 500);
	}));
}

/* ---- init ------------------------------------------------------------ */

async function init() {
	$$(".drop").forEach(setup_drop);
	setup_inputs();
	setup_params();
	setup_settings();
	$("#btn-convert").addEventListener("click", convert);
	$("#btn-save-voice").addEventListener("click", save_voice);
	$("#btn-clear-jobs").addEventListener("click", clear_jobs);
	$("#model").addEventListener("change", update_convert_button);

	await Promise.all([load_models(), load_examples(), load_voices()]);
	try {
		state.uploads = await api("api/uploads");
	} catch (e) {
		state.uploads = [];
	}
	render_recent();
	await poll_status();
	await poll_jobs();
	if (has_active_jobs())
		start_job_polling();
	setInterval(poll_status, POLL_STATUS_MS);
	update_convert_button();
}

document.addEventListener("DOMContentLoaded", init);
