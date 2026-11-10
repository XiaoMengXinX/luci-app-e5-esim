'use strict';
'require view';
'require rpc';
'require ui';

var callSnapshot = rpc.declare({ object: 'e5-esim', method: 'snapshot', params: [ 'slot', 'aid', 'scan' ], nobatch: true });
var callNotifications = rpc.declare({ object: 'e5-esim', method: 'notifications', params: [ 'slot', 'aid' ], nobatch: true });
var callDownloadStart = rpc.declare({ object: 'e5-esim', method: 'download_start', params: [ 'slot', 'activation', 'confirmation', 'aid' ], nobatch: true });
var callOperationStart = rpc.declare({ object: 'e5-esim', method: 'operation_start', params: [ 'slot', 'request', 'aid' ], nobatch: true });
var callJobStatus = rpc.declare({ object: 'e5-esim', method: 'job_status', nobatch: true });
var callDependencies = rpc.declare({ object: 'e5-esim', method: 'dependencies', nobatch: true });
var callJobConfirm = rpc.declare({ object: 'e5-esim', method: 'job_confirm', params: [ 'accept' ], nobatch: true });
var callJobCancel = rpc.declare({ object: 'e5-esim', method: 'job_cancel', nobatch: true });

var SE_AIDS = [ 'A06573746B6D65FFFF4953442D522030', 'A06573746B6D65FFFF4953442D522031' ];

var LANG = String((L.env && L.env.lang) || document.documentElement.lang || 'en').toLowerCase();
var ZH = LANG.indexOf('zh') === 0;

function tr(zh, en) {
	return ZH ? zh : en;
}

var CSS = `
#modal_overlay>.modal.e5-esim-modal{box-sizing:border-box;width:min(42rem,calc(100vw - 2rem));max-width:calc(100vw - 2rem);min-width:0}
.e5-esim>.cbi-tabmenu{margin-bottom:12px}
.e5-esim>.e5-view-root{display:flex;flex-direction:column;gap:12px}
.e5-esim>.e5-view-root>.cbi-section{margin:0}
.e5-esim .e5-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.e5-esim .e5-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.e5-esim .e5-form{max-width:840px}
.e5-esim .e5-dialog{display:flex;flex-direction:column;gap:12px}
.e5-esim .e5-dialog>.e5-info-row{margin:0;padding-block:0}
.e5-esim .e5-dialog>.e5-actions{justify-content:flex-end;float:none}
.e5-esim .e5-info-row{align-items:baseline}
.e5-esim .e5-info-row>.cbi-value-title{padding-top:0}
.e5-esim .e5-info-row>.cbi-value-field{min-width:0}
.e5-esim .e5-progress-label{display:flex;justify-content:space-between;gap:12px}
.e5-esim .e5-table-wrap{overflow-x:auto}
.e5-esim .e5-id{overflow-wrap:anywhere;font-family:monospace}
.e5-esim .e5-command{overflow-x:auto;margin:12px 0;padding:10px 12px;background:var(--background-color-low,rgba(127,127,127,.08));border-radius:4px}
.e5-esim .e5-deps{margin:0;padding-left:20px}
.e5-esim .e5-step{margin:0}
.e5-esim .e5-actions .cbi-button{white-space:nowrap}
`;

var STEPS = {
	'es10b_get_euicc_challenge_and_info': tr('正在验证 eSIM…', 'Verifying the eSIM…'),
	'es9p_initiate_authentication': tr('正在连接服务器…', 'Connecting to the server…'),
	'es10b_authenticate_server': tr('正在验证服务器…', 'Verifying the server…'),
	'es9p_authenticate_client': tr('正在认证…', 'Authenticating…'),
	// lpac 2.3.0（apk）把这一步拼作 es8p_meatadata_parse，新版改回了正确拼写。
	'es8p_metadata_parse': tr('正在解析配置文件…', 'Parsing the profile…'),
	'es8p_meatadata_parse': tr('正在解析配置文件…', 'Parsing the profile…'),
	'es10b_prepare_download': tr('正在准备下载…', 'Preparing the download…'),
	'es9p_get_bound_profile_package': tr('正在获取配置文件…', 'Fetching the profile…'),
	'es10b_load_bound_profile_package': tr('正在写入 eSIM…', 'Writing to the eSIM…'),
	'es10b_cancel_session': tr('正在取消会话…', 'Cancelling the session…'),
	'es9p_cancel_session': tr('正在取消会话…', 'Cancelling the session…')
};

function downloadStep(step) {
	return STEPS[step] || tr('正在处理…', 'Working…');
}

function operationName(operation) {
	if (operation === 'install')
		return tr('安装', 'Install');
	if (operation === 'enable')
		return tr('启用', 'Enable');
	if (operation === 'disable')
		return tr('停用', 'Disable');
	if (operation === 'delete')
		return tr('删除', 'Delete');
	return operation;
}

function failureText(error, detail) {
	error = error || '';
	detail = detail || '';
	if (error === 'euicc_init')
		return tr('无法访问 eSIM', 'Cannot reach the eSIM');
	if (error === 'activation_code' || error === 'matching_id')
		return tr('激活码无效', 'The activation code is invalid');
	if (error === 'confirmation_code' && detail === 'required')
		return tr('该激活码需要确认码', 'This activation code requires a confirmation code');
	if (error === 'es10c_get_profiles_info' || error === 'es10b_list_notification')
		return tr('读取 eSIM 失败', 'Cannot read the eSIM');
	if ((error.indexOf('es9p_') === 0 || error.indexOf('es8p_') === 0) &&
	    (detail.indexOf('HTTP') >= 0 || detail.indexOf('http') >= 0))
		return tr('服务器通信失败', 'Cannot reach the server');
	if (detail.indexOf('iccid or aid not found') >= 0)
		return tr('找不到该配置文件', 'The profile was not found');
	if (detail.indexOf('profile not in disabled state') >= 0)
		return tr('请先停用该配置文件', 'Disable the profile first');
	if (detail.indexOf('profile not in enabled state') >= 0)
		return tr('该配置文件未启用', 'The profile is not enabled');
	if (detail.indexOf('disallowed by policy') >= 0)
		return tr('eSIM 策略不允许', 'The eSIM policy does not allow it');
	if (detail.indexOf('timeout') >= 0 || detail.indexOf('Timeout') >= 0)
		return tr('操作超时', 'The operation timed out');
	if (error === 'lpac')
		return tr('操作失败，请重试', 'The operation failed; try again');
	if (error === 'busy')
		return tr('eSIM 正忙，请稍后再试', 'The eSIM is busy; try again later');
	return detail || error;
}

function profileName(profile) {
	return profile.profileNickname || profile.profileName || profile.iccid || '--';
}

function jget(value, path) {
	var parts = path.split('.');
	for (var i = 0; i < parts.length && value != null; i++)
		value = value[parts[i]];
	return value;
}

function bytes(n) {
	var units = [ 'B', 'KB', 'MB', 'GB', 'TB' ], i = 0;
	while (n >= 1024 && i < 4) {
		n /= 1024;
		i++;
	}
	return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
}

function createPage(loaded) {
	var deps = loaded && loaded.deps;
	var job = loaded && loaded.job;
	var state = {
		view: 'loading', action: null, current: 0, pick: true,
		iccid: '', name: '', enabled: false,
		activation: '', confirmation: '',
		downloading: false, cancelling: false,
		metadata: null, seq: 0, disposed: false, resuming: false, deps: deps || null
	};
	var slots = [ newSlot(), newSlot() ];
	var bulk = { total: 0, next: 0, remove: false, running: false };
	var pollTimer = null, openModal = false, previewShown = false;
	var restoreSEFocus = false, selectedSEControl = null;
	var stepNode = null;
	var downloadMeter = null, downloadFill = null, writeValue = null, writeBytes = null;

	function newSlot() {
		return { chip: null, profiles: null, notifications: null, chipError: '', aid: '', ses: 0, scanned: false };
	}

	function toast(text) {
		ui.addNotification(null, E('p', {}, text), 'info');
	}

	function toastFailure(error, detail) {
		ui.addNotification(null, E('p', {}, failureText(error, detail)), 'danger');
	}

	function openDialog(title, children) {
		openModal = true;
		ui.showModal(title, [ E('div', { class: 'e5-esim' }, children) ], 'e5-esim-modal');
	}

	function closeDialog() {
		if (!openModal)
			return;
		openModal = false;
		ui.hideModal();
	}

	function button(label, kind, onclick) {
		return E('button', { type: 'button', class: 'btn cbi-button' + (kind ? ' cbi-button-' + kind : ''),
			disabled: kind && !L.hasViewPermission() ? '' : null, click: onclick }, label);
	}

	function cancelButton(onclick) {
		var node = button(tr('取消', 'Cancel'), '', onclick);
		node.setAttribute('data-esim-cancel', '');
		return node;
	}

	function sheet(children) {
		var content = [], actions = [];
		children.forEach(function(child) {
			if (child == null) return;
			if (child.tagName !== 'BUTTON') content.push(child);
			else if (child.hasAttribute('data-esim-cancel')) actions.unshift(child);
			else actions.push(child);
		});
		if (actions.length) content.push(E('div', { class: 'right e5-actions' }, actions));
		return E('div', { class: 'e5-dialog' }, content);
	}

	function message(text) {
		return E('div', { class: 'cbi-section-descr' }, text);
	}

	function note(text) {
		return E('p', { class: 'cbi-section-descr' }, text);
	}

	function card(children) {
		return E('section', { class: 'cbi-section' }, children);
	}

	function kv(label, value) {
		return E('div', { class: 'cbi-value e5-info-row' }, [
			E('div', { class: 'cbi-value-title' }, label),
			E('div', { class: 'cbi-value-field e5-id' }, value)
		]);
	}

	function heading(text) {
		return E('h3', {}, text);
	}

	function dependencyItems() {
		return ((state.deps && state.deps.items) || []).filter(function(item) { return !item.present; });
	}

	function dependencyMissing(name) {
		return !!state.deps && (state.deps.missing || []).indexOf(name) >= 0;
	}

	function installCommand() {
		var data = state.deps || {};
		var packages = data.missing && data.missing.length ? data.missing : [ 'lpac', 'libcurl-gnutls4' ];
		return 'apk update && apk add ' + packages.join(' ');
	}

	function softwareLink() {
		var parts = [ 'admin', 'system', 'opkg' ];
		var href = typeof L.url === 'function' ? L.url.apply(L, parts) : '/cgi-bin/luci/' + parts.join('/');
		return E('a', { class: 'btn cbi-button', href: href }, tr('打开软件包管理', 'Open package manager'));
	}

	function dependencyCard() {
		var missing = dependencyItems();
		var blocking = dependencyMissing('lpac');
		var nodes = [ E('div', { class: 'e5-toolbar' }, [ heading(tr('运行依赖', 'Runtime dependencies')),
			E('div', { class: 'e5-actions' }, [
				softwareLink(),
				button(tr('重新检测', 'Check again'), 'apply', checkDependencies)
			]) ]),
			message(blocking
				? tr('缺少 lpac，无法访问 eSIM。安装依赖后重新检测。', 'lpac is missing, so the eSIM cannot be accessed. Install the dependencies and check again.')
				: tr('缺少 libcurl-gnutls4，配置文件下载可能失败；其他功能仍可使用。', 'libcurl-gnutls4 is missing; profile downloads may fail. Other functions still work.')) ];
		if (missing.length)
			nodes.push(E('ul', { class: 'e5-deps' }, missing.map(function(item) {
				return E('li', {}, [ E('code', {}, item.package), ' ', item.path || '' ]);
			})));
		nodes.push(E('pre', { class: 'e5-command' }, installCommand()));
		nodes.push(note(tr('安装后重新检测；也可在上面的软件包管理中安装。', 'Check again after installing, or use the package manager link above.')));
		return card(nodes);
	}

	function checkDependencies() {
		state.view = 'loading';
		render();
		callDependencies().then(function(result) {
			if (state.disposed)
				return;
			state.deps = result && result.ok ? result.data : null;
			if (dependencyMissing('lpac')) {
				state.view = 'deps';
				render();
				return;
			}
			load();
		}).catch(function() {
			if (state.disposed)
				return;
			state.deps = null;
			load();
		});
	}

	function backCard() {
		return E('div', { class: 'e5-actions' }, [ button(tr('返回', 'Back'), '', goRoot) ]);
	}

	var tabs = [ 0, 1 ].map(function(slot) {
		return E('a', { href: '#', role: 'button', click: function(event) {
			event.preventDefault();
			if (state.view === 'root' || state.view === 'notifs') selectSlot(slot);
		} }, slot ? 'SIM 2' : 'SIM 1');
	});
	var tabItems = tabs.map(function(tab) { return E('li', {}, tab); });
	var root = E('div', { class: 'cbi-map e5-esim' }, [ E('style', {}, CSS),
		E('h2', {}, tr('eSIM 管理', 'eSIM management')),
		E('div', { class: 'cbi-map-descr' }, tr('管理配置文件、下载与通知', 'Manage profiles, downloads and notifications')),
		E('ul', { class: 'cbi-tabmenu' }, tabItems) ]);
	var body = E('div');
	root.appendChild(body);

	function paintTabs() {
		var switchable = state.view === 'root' || state.view === 'notifs';
		tabs.forEach(function(tab, slot) {
			tab.setAttribute('aria-disabled', String(!switchable));
			tab.tabIndex = switchable ? 0 : -1;
			tabItems[slot].className = slot === state.current ? 'cbi-tab' : 'cbi-tab-disabled';
		});
	}

	function render() {
		if (state.disposed)
			return;
		paintTabs();
		selectedSEControl = null;
		body.className = 'e5-view-' + state.view;
		body.replaceChildren.apply(body, viewNodes());
		if (restoreSEFocus && selectedSEControl) {
			selectedSEControl.focus();
			restoreSEFocus = false;
		}
	}

	function viewNodes() {
		switch (state.view) {
		case 'loading':
			return [ message(tr('正在读取 eSIM 信息…', 'Reading the eSIM…')) ];
		case 'notif_loading':
			return [ message(tr('正在读取通知…', 'Reading notifications…')) ];
		case 'root':
			return rootNodes();
		case 'deps':
			return [ dependencyCard() ];
		case 'download':
			return downloadNodes();
		case 'rename':
			return renameNodes();
		case 'notifs':
			return notifsNodes();
		case 'info':
			return infoNodes();
		case 'wait':
			return [ message(waitText()) ];
		}
		return [];
	}

	function waitText() {
		if (bulk.running)
			return (bulk.remove ? tr('正在删除通知 %d/%d…', 'Deleting notification %d/%d…')
			                    : tr('正在处理通知 %d/%d…', 'Handling notification %d/%d…'))
				.replace('%d', String(bulk.next)).replace('%d', String(bulk.total));
		switch (state.action) {
		case 'switch':
			return tr('正在切换配置文件…', 'Switching profile…');
		case 'rename':
			return tr('正在重命名…', 'Renaming…');
		case 'delete':
			return tr('正在删除配置文件…', 'Deleting the profile…');
		case 'download':
			return state.cancelling ? tr('正在取消下载…', 'Cancelling download…')
			                        : tr('正在上报下载通知…', 'Reporting download…');
		case 'notifications':
			return tr('正在处理通知…', 'Handling the notification…');
		}
		return '';
	}

	function rootNodes() {
		var slot = slots[state.current];
		var nodes = [];
		if (dependencyItems().length)
			nodes.push(dependencyCard());
		if (slot.ses) {
			var choices = [];
			SE_AIDS.forEach(function(aid, index) {
				if (!(slot.ses & (1 << index))) return;
				var selected = slot.aid === aid;
				var control = button('SE ' + index, '', function() { selectSE(aid); });
				control.setAttribute('class', 'btn cbi-button' + (selected ? ' cbi-button-apply' : ''));
				control.setAttribute('aria-pressed', String(selected));
				if (selected) selectedSEControl = control;
				choices.push(control);
			});
			nodes.push(card([ E('div', { class: 'e5-toolbar' }, [ heading(tr('选择 SE', 'Select SE')),
				E('div', { class: 'e5-actions', role: 'group', 'aria-label': tr('选择 SE', 'Select SE') }, choices) ]) ]));
		}
		if (!slot.chip && !slot.profiles)
			return nodes.concat([ card([ message(slot.chipError === 'euicc_init'
				? tr('该卡槽没有 eUICC', 'No eUICC in this slot')
				: tr('无法读取 eSIM 信息', 'Cannot read the eSIM')),
				button(tr('重试', 'Retry'), 'apply', load) ]) ]);
		var chip = slot.chip || {};
		var summary = card([
			E('div', { class: 'e5-toolbar' }, [ heading(tr('eSIM 芯片', 'eSIM chip')),
				button(tr('详情 ›', 'Details ›'), '', openInfo) ]),
			kv('EID', jget(chip, 'eidValue') || '--'),
			kv(tr('固件', 'Firmware'), jget(chip, 'EUICCInfo2.euiccFirmwareVer') || '--')
		]);
		var panel = [ E('div', { class: 'e5-toolbar' }, [ heading(tr('配置文件', 'Profiles')),
			E('div', { class: 'e5-actions' }, [
				button(tr('通知', 'Notifications') + ' (' + (slot.notifications || []).length + ')', '', openNotifications),
				button(tr('添加配置文件', 'Add profile'), 'apply', openDownload)
			]) ]) ];
		if (!slot.profiles) panel.push(message(tr('读取 eSIM 失败', 'Cannot read the eSIM')), button(tr('重试', 'Retry'), 'apply', load));
		else {
			var columns = [ tr('名称', 'Name'), tr('运营商', 'Provider'), 'ICCID', tr('状态', 'Status'), tr('操作', 'Actions') ];
			var rows = slot.profiles.map(function(profile, index) {
				var manage = button(tr('管理', 'Manage'), '', function() { openProfile(index); });
				manage.setAttribute('aria-label', profileName(profile));
				return E('tr', { class: 'tr' }, [
					E('td', { class: 'td', 'data-title': columns[0] }, profileName(profile)),
					E('td', { class: 'td', 'data-title': columns[1] }, profile.serviceProviderName || '--'),
					E('td', { class: 'td e5-id', 'data-title': 'ICCID' }, profile.iccid || '--'),
					E('td', { class: 'td', 'data-title': columns[3] }, profile.profileState === 'enabled' ? tr('已启用', 'Enabled') : tr('未启用', 'Disabled')),
					E('td', { class: 'td cbi-section-actions' }, manage)
				]);
			});
			panel.push(E('div', { class: 'e5-table-wrap' }, E('table', { class: 'table cbi-section-table' }, [
				E('thead', {}, E('tr', { class: 'tr table-titles' }, columns.map(function(label) { return E('th', { class: 'th' }, label); }))),
				E('tbody', {}, rows)
			])));
			if (!rows.length) panel.push(note(tr('尚未安装配置文件', 'No profiles installed')));
		}
		return nodes.concat([ summary, card(panel) ]);
	}

	function downloadNodes() {
		return [ E('div', { class: 'e5-form' }, [ card([
			heading(tr('添加配置文件', 'Add profile')),
			E('div', { class: 'cbi-value' }, [ E('label', { class: 'cbi-value-title', for: 'e5-activation' }, tr('激活码', 'Activation code')),
				E('div', { class: 'cbi-value-field' }, E('input', { id: 'e5-activation', type: 'text', class: 'cbi-input-text', placeholder: 'LPA:1$…', value: state.activation,
					autocomplete: 'off', spellcheck: 'false', input: function(ev) { state.activation = ev.target.value; } })) ]),
			E('div', { class: 'cbi-value' }, [ E('label', { class: 'cbi-value-title', for: 'e5-confirmation' }, tr('确认码（可选）', 'Confirmation code (optional)')),
				E('div', { class: 'cbi-value-field' }, E('input', { id: 'e5-confirmation', type: 'password', class: 'cbi-input-text', value: state.confirmation,
					autocomplete: 'off', input: function(ev) { state.confirmation = ev.target.value; } })) ])
		]), E('div', { class: 'cbi-page-actions e5-actions' }, [ button(tr('返回', 'Back'), '', goRoot), button(tr('下载', 'Download'), 'apply', startDownload) ]) ]) ];
	}

	function renameNodes() {
		return [ E('div', { class: 'e5-form' }, [ card([
			heading(tr('重命名', 'Rename')),
			E('div', { class: 'cbi-value' }, [ E('label', { class: 'cbi-value-title', for: 'e5-name' }, tr('名称', 'Name')),
				E('div', { class: 'cbi-value-field' }, E('input', { id: 'e5-name', type: 'text', class: 'cbi-input-text', maxlength: '64', value: state.name,
					input: function(ev) { state.name = ev.target.value; } })) ])
		]), E('div', { class: 'cbi-page-actions e5-actions' }, [ button(tr('返回', 'Back'), '', goRoot), button(tr('保存', 'Save'), 'apply', saveName) ]) ]) ];
	}

	function notifsNodes() {
		var list = slots[state.current].notifications || [];
		var content = [ E('div', { class: 'e5-toolbar' }, [ heading(tr('通知', 'Notifications')),
			E('div', { class: 'e5-actions' }, list.length ? [
				button(tr('处理全部', 'Process all'), 'apply', processAll),
				button(tr('全部删除', 'Delete all'), 'remove', confirmRemoveAll)
			] : []) ]) ];
		if (!list.length) content.push(message(tr('没有待处理的通知', 'No pending notifications')));
		else {
			var columns = [ tr('通知', 'Notifications'), 'ICCID', tr('服务器', 'Server'), tr('操作', 'Actions') ];
			content.push(E('div', { class: 'e5-table-wrap' }, E('table', { class: 'table cbi-section-table' }, [
				E('thead', {}, E('tr', { class: 'tr table-titles' }, columns.map(function(label) { return E('th', { class: 'th' }, label); }))),
				E('tbody', {}, list.map(function(notification, index) {
					return E('tr', { class: 'tr' }, [
						E('td', { class: 'td', 'data-title': columns[0] }, operationName(notification.profileManagementOperation || '') + ' #' + notification.seqNumber),
						E('td', { class: 'td e5-id', 'data-title': 'ICCID' }, notification.iccid || '--'),
						E('td', { class: 'td', 'data-title': columns[2] }, notification.notificationAddress || '--'),
						E('td', { class: 'td cbi-section-actions' }, button(tr('管理', 'Manage'), '', function() { openNotification(index); }))
					]);
				}))
			])));
		}
		return [ card(content), backCard() ];
	}

	function infoField(chip, label, path) {
		var value = jget(chip, path);
		if (typeof value === 'string')
			return kv(label, value);
		if (typeof value === 'number')
			return kv(label, bytes(value));
		if (Array.isArray(value))
			return kv(label, value.length ? value.map(function(item) { return E('div', {}, item); }) : '--');
		return kv(label, '--');
	}

	function infoNodes() {
		var chip = slots[state.current].chip || {};
		return [
			card([ infoField(chip, 'EID', 'eidValue'),
				infoField(chip, tr('固件版本', 'Firmware'), 'EUICCInfo2.euiccFirmwareVer'),
				infoField(chip, tr('SGP.22 版本', 'SGP.22 version'), 'EUICCInfo2.svn'),
				infoField(chip, tr('配置格式版本', 'Profile format version'), 'EUICCInfo2.profileVersion'),
				infoField(chip, tr('GlobalPlatform 版本', 'GlobalPlatform version'), 'EUICCInfo2.globalplatformVersion') ]),
			card([ infoField(chip, tr('剩余存储空间', 'Free storage'), 'EUICCInfo2.extCardResource.freeNonVolatileMemory'),
				infoField(chip, tr('剩余运行内存', 'Free RAM'), 'EUICCInfo2.extCardResource.freeVolatileMemory') ]),
			card([ infoField(chip, tr('签名证书公钥标识', 'Signing certificate key IDs'), 'EUICCInfo2.euiccCiPKIdListForSigning'),
				infoField(chip, tr('验证证书公钥标识', 'Verification certificate key IDs'), 'EUICCInfo2.euiccCiPKIdListForVerification'),
				infoField(chip, tr('SAS 认证编号', 'SAS accreditation'), 'EUICCInfo2.sasAcreditationNumber') ]),
			backCard()
		];
	}

	function poll(fn) {
		pollTimer = window.setTimeout(function() {
			pollTimer = null;
			callJobStatus().then(function(job) {
				if (state.disposed)
					return;
				if (fn(job) !== false)
					poll(fn);
			}).catch(function() {
				if (!state.disposed)
					poll(fn);
			});
		}, 1000);
	}

	function stopPoll() {
		if (pollTimer) {
			window.clearTimeout(pollTimer);
			pollTimer = null;
		}
	}

	function goRoot() {
		state.view = 'root';
		render();
	}

	function selectSE(aid) {
		var slot = slots[state.current];
		if (slot.aid === aid) return;
		state.pick = false;
		slot.aid = aid;
		restoreSEFocus = true;
		state.view = 'loading';
		render();
		load();
	}

	function selectSlot(slot) {
		state.pick = false;
		state.current = slot;
		load();
	}

	function load() {
		stopPoll();
		var slot = slots[state.current];
		slot.chip = slot.profiles = slot.notifications = null;
		slot.chipError = '';
		state.view = 'loading';
		render();
		snap(state.current);
	}

	function snap(slot) {
		var selected = slots[slot];
		var scan = !selected.scanned;
		callSnapshot(slot, selected.aid, scan).then(function(r) {
			if (state.disposed)
				return;
			var target = slots[slot];
			if (!r.ok) {
				if (state.pick) {
					state.pick = false;
					state.current = 1;
					snap(1);
					return;
				}
				target.chipError = r.error || '';
				goRoot();
				return;
			}
			state.pick = false;
			target.aid = r.aid || '';
			if (typeof r.ses === 'number') target.ses = r.ses;
			if (scan) target.scanned = true;
			target.chip = r.chip.data;
			if (r.profiles && r.profiles.ok)
				target.profiles = r.profiles.data || [];
			else if (r.profiles)
				toastFailure(r.profiles.error, r.profiles.detail);
			if (r.notifications && r.notifications.ok)
				target.notifications = r.notifications.data || [];
			goRoot();
		});
	}

	function openInfo() {
		state.view = 'info';
		render();
	}

	function openNotifications() {
		state.view = 'notif_loading';
		render();
		callNotifications(state.current, slots[state.current].aid).then(onNotifList);
	}

	function onNotifList(r) {
		if (state.disposed)
			return;
		if (r.ok)
			slots[state.current].notifications = r.data || [];
		else
			toastFailure(r.error, r.detail);
		state.view = 'notifs';
		render();
	}

	function openProfile(index) {
		var profile = slots[state.current].profiles[index];
		state.iccid = profile.iccid || '';
		state.name = profileName(profile);
		state.enabled = profile.profileState === 'enabled';
		openDialog(profileName(profile), [ sheet([
			kv(tr('运营商', 'Provider'), profile.serviceProviderName || '--'),
			kv('ICCID', state.iccid),
			button(state.enabled ? tr('停用', 'Disable') : tr('启用', 'Enable'), 'apply', confirmSwitch),
			button(tr('重命名', 'Rename'), '', openRename),
			state.enabled ? null : button(tr('删除', 'Delete'), 'remove', confirmDelete),
			cancelButton(closeDialog)
		]) ]);
	}

	function confirmSwitch() {
		var label = state.enabled ? tr('停用', 'Disable') : tr('启用', 'Enable');
		openDialog(label, [ sheet([
			note(state.enabled
				? tr('停用后该配置文件不可用，基带将重新读取 SIM 卡', 'The profile becomes unusable and the modem re-reads the SIM')
				: tr('将停用当前启用的配置文件，并让基带重新读取 SIM 卡', 'The current profile turns off and the modem re-reads the SIM')),
			button(label, 'apply', doSwitch),
			cancelButton(closeDialog)
		]) ]);
	}

	function doSwitch() {
		closeDialog();
		startOperation({ action: 'switch', iccid: state.iccid, enabled: !state.enabled });
	}

	function warnJob(job) {
		if (job.profile_state_pending)
			toast(tr('配置状态尚未更新，请稍后重试', 'Profile state has not updated; try again later'));
		if (job.warning === 'refresh')
			toast(tr('SIM 刷新失败', 'SIM refresh failed'));
		else if (job.warning === 'notification_list')
			toast(tr('操作已完成，读取通知失败，可在通知页重试', 'Operation completed; open Notifications to retry reporting'));
		else if (job.warning === 'notification_report')
			toast(tr('操作已完成，部分通知尚未上报，可在通知页重试', 'Operation completed; retry pending notifications from Notifications'));
	}

	function watchOperation() {
		poll(function(job) {
			if (job.running) {
				if (job.step === 'notifications') {
					bulk.running = true;
					bulk.next = job.current;
					bulk.total = job.total;
				}
				render();
				return;
			}
			bulk.running = false;
			warnJob(job);
			if (job.state !== 'done') {
				toastFailure(job.error || 'lpac', job.detail);
				if (state.action === 'notifications') {
					state.view = 'notifs';
					render();
				}
				else goRoot();
				return false;
			}
			if (state.resuming) {
				state.resuming = false;
				load();
				return false;
			}
			if (state.action === 'notifications') {
				if (job.failed)
					toast(tr('部分通知未能处理，可稍后重试', 'Some notifications could not be handled; try again later'));
				else
					toast((bulk.remove ? tr('已删除 %d 条通知', 'Deleted %d notifications') : tr('已处理 %d 条通知', 'Handled %d notifications')).replace('%d', String(job.processed)));
				onNotifList({ ok: true, data: job.data });
			}
			else if (state.action === 'delete') load();
			else {
				slots[state.current].profiles = job.data || [];
				goRoot();
			}
			return false;
		});
	}

	function startOperation(request) {
		state.action = request.action;
		state.view = 'wait';
		bulk.remove = !!request.remove;
		render();
		callOperationStart(state.current, JSON.stringify(request), slots[state.current].aid).then(function(result) {
			if (state.disposed) return;
			if (!result.ok) {
				toastFailure(result.error, result.detail);
				goRoot();
				return;
			}
			watchOperation();
		});
	}

	function openRename() {
		closeDialog();
		state.view = 'rename';
		render();
	}

	function saveName() {
		startOperation({ action: 'rename', iccid: state.iccid, name: state.name });
	}

	function confirmDelete() {
		openDialog(tr('删除配置文件', 'Delete the profile'), [ sheet([
			note(tr('删除后无法恢复', 'This cannot be undone')),
			button(tr('删除', 'Delete'), 'remove', doDelete),
			cancelButton(closeDialog)
		]) ]);
	}

	function doDelete() {
		closeDialog();
		startOperation({ action: 'delete', iccid: state.iccid });
	}

	function openDownload() {
		state.view = 'download';
		render();
	}

	function openProgress(text) {
		stepNode = E('p', { class: 'e5-step', 'aria-live': 'polite' }, text || tr('正在处理…', 'Working…'));
		downloadFill = E('div', { style: 'width:0%' });
		downloadMeter = E('div', { class: 'cbi-progressbar', role: 'progressbar', 'aria-label': tr('写入进度', 'Write progress'),
			'aria-valuemin': 0, 'aria-valuemax': 100 }, downloadFill);
		writeValue = E('strong');
		writeBytes = E('span', {}, tr('等待服务器响应', 'Waiting for the server'));
		openDialog(tr('正在下载配置文件', 'Downloading the profile'), [ sheet([
			stepNode,
			E('div', { class: 'e5-progress-label' }, [ writeBytes, writeValue ]),
			downloadMeter,
			cancelButton(cancelDownload)
		]) ]);
	}

	function updateDownloadProgress(job) {
		if (!stepNode || !downloadMeter)
			return;
		var writing = job.step === 'es10b_load_bound_profile_package';
		var write = writing && job.write;
		stepNode.textContent = downloadStep(job.step);
		if (write && write.total > 0) {
			downloadFill.style.width = write.percent + '%';
			downloadMeter.setAttribute('aria-valuenow', write.percent);
			writeValue.textContent = write.percent + '%';
			writeBytes.textContent = bytes(write.written) + ' / ' + bytes(write.total);
			if (write.percent === 100) {
				stepNode.textContent = tr('正在确认安装结果…', 'Confirming the installation…');
			}
		}
		else {
			downloadFill.style.width = '0%';
			downloadMeter.removeAttribute('aria-valuenow');
			writeValue.textContent = '';
			writeBytes.textContent = writing ? tr('正在准备写入数据', 'Preparing profile data')
				: tr('正在等待响应…', 'Waiting for a response…');
		}
	}

	function startDownload() {
		if (!state.activation) {
			toast(tr('请输入激活码', 'Enter the activation code'));
			return;
		}
		state.action = 'download';
		state.downloading = true;
		state.cancelling = false;
		previewShown = false;
		openProgress(null);
		callDownloadStart(state.current, state.activation, state.confirmation, slots[state.current].aid).then(function(result) {
			if (state.disposed) return;
			if (!result.ok) {
				finishDownload(false, result.error, result.detail);
				return;
			}
			if (state.cancelling) callJobCancel();
			watchDownload();
		});
	}

	function watchDownload() {
		poll(function(job) {
			if (!job.running) {
				warnJob(job);
				finishDownload(job.state === 'done', job.error || 'lpac', job.detail);
				return false;
			}
			if (job.metadata)
				state.metadata = job.metadata;
			if (state.cancelling)
				return;
			if (job.step === 'reporting' || job.step === 'notifications') {
				closeDialog();
				state.view = 'wait';
				render();
				return;
			}
			if (job.preview) {
				if (!previewShown) {
					previewShown = true;
					confirmDownload();
				}
			}
			else {
				previewShown = false;
				updateDownloadProgress(job);
			}
		});
	}

	function confirmDownload() {
		var metadata = state.metadata || {};
		openDialog(tr('确认下载配置文件', 'Confirm profile download'), [ sheet([
			kv(tr('运营商', 'Provider'), metadata.serviceProviderName || '--'),
			kv(tr('配置名称', 'Profile name'), metadata.profileName || '--'),
			kv('ICCID', metadata.iccid || '--'),
			kv(tr('配置类别', 'Profile class'), metadata.profileClass || '--'),
			button(tr('确认下载', 'Download'), 'apply', acceptDownload),
			cancelButton(cancelDownload)
		]) ]);
	}

	function acceptDownload() {
		callJobConfirm(true).then(function(r) {
			if (state.disposed)
				return;
			if (!r.ok) {
				toast(tr('无法继续下载', 'Cannot continue the download'));
				return;
			}
			openProgress(null);
		});
	}

	function cancelDownload() {
		closeDialog();
		state.cancelling = true;
		state.view = 'wait';
		render();
		callJobCancel();
	}

	function finishDownload(ok, error, detail) {
		if (state.disposed)
			return;
		state.downloading = false;
		if (!state.cancelling)
			closeDialog();
		stepNode = null;
		state.metadata = null;
		if (!ok) {
			if (state.cancelling)
				toast(tr('已取消下载', 'Download cancelled'));
			else
				toastFailure(error, detail);
			state.cancelling = false;
			state.view = 'download';
			render();
			return;
		}
		state.cancelling = false;
		state.activation = '';
		state.confirmation = '';
		toast(tr('配置文件已下载', 'Profile downloaded'));
		load();
	}

	function openNotification(index) {
		var notification = slots[state.current].notifications[index];
		state.seq = notification.seqNumber || 0;
		openDialog(tr('通知', 'Notifications'), [ sheet([
			note(notification.notificationAddress || ''),
			note(notification.iccid || ''),
			button(tr('处理', 'Process'), 'apply', processNotification),
			button(tr('删除', 'Delete'), 'remove', removeNotification),
			cancelButton(closeDialog)
		]) ]);
	}

	function processNotification() {
		closeDialog();
		startOperation({ action: 'notifications', notifications: [{ seqNumber: state.seq }], remove: false, single: true });
	}

	function removeNotification() {
		closeDialog();
		startOperation({ action: 'notifications', notifications: [{ seqNumber: state.seq }], remove: true, single: true });
	}

	function processAll() {
		bulkStart(false);
	}

	function confirmRemoveAll() {
		openDialog(tr('全部删除通知', 'Delete all notifications'), [ sheet([
			note(tr('删除后无法恢复', 'This cannot be undone')),
			button(tr('删除', 'Delete'), 'remove', removeAll),
			cancelButton(closeDialog)
		]) ]);
	}

	function removeAll() {
		closeDialog();
		bulkStart(true);
	}

	function bulkStart(remove) {
		startOperation({ action: 'notifications', notifications: slots[state.current].notifications || [], remove: remove });
	}

	if (dependencyMissing('lpac')) {
		state.view = 'deps';
		render();
	}
	else if (job && job.running && job.type === 'download') {
		state.current = job.slot;
		slots[state.current].aid = job.aid || '';
		state.action = 'download';
		state.downloading = true;
		state.view = 'wait';
		render();
		openProgress(null);
		watchDownload();
	}
	else if (job && job.running && job.type === 'operation') {
		state.current = job.slot;
		slots[state.current].aid = job.aid || '';
		state.resuming = true;
		state.action = job.action;
		bulk.remove = !!job.remove;
		state.view = 'wait';
		render();
		watchOperation();
	}
	else {
		load();
	}

	return {
		root: root,
		stop: function() {
			state.disposed = true;
			stopPoll();
			closeDialog();
		}
	};
}

return view.extend({
	load: function() {
		return Promise.all([
			callDependencies().catch(function() { return null; }),
			callJobStatus().catch(function() { return null; })
		]).then(function(results) {
			var dependencies = results[0];
			return { deps: dependencies && dependencies.ok ? dependencies.data : null, job: results[1] };
		});
	},

	render: function(loaded) {
		var page = createPage(loaded || {});
		this.stop = page.stop;
		return page.root;
	},

	unload: function() {
		if (this.stop)
			this.stop();
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
