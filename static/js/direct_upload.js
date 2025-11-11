(function () {
  const config = (window.BenlabUploadConfig || {});
  if (!config.enabled || !config.presign_url) {
    return;
  }
  const fieldSuffix = config.field_suffix || '_remote_keys';

  class DirectOssUploader {
    constructor(input) {
      this.input = input;
      this.form = input.closest('form');
      this.remoteFieldName = input.dataset.remoteField || (input.name ? `${input.name}${fieldSuffix}` : null);
      if (!this.form || !this.remoteFieldName) {
        return;
      }
      this.uploading = false;
      this.entries = [];
      this.hiddenContainer = document.createElement('div');
      this.hiddenContainer.className = 'direct-upload-hidden-inputs';
      this.hiddenContainer.hidden = true;
      this.form.appendChild(this.hiddenContainer);
      this.statusEl = document.createElement('div');
      this.statusEl.className = 'form-text text-muted mt-1';
      this.input.insertAdjacentElement('afterend', this.statusEl);
      this.listEl = document.createElement('div');
      this.listEl.className = 'small direct-upload-list mt-2';
      this.statusEl.insertAdjacentElement('afterend', this.listEl);
      this.bindEvents();
    }

    bindEvents() {
      this.input.addEventListener('change', () => {
        if (!this.input.files || !this.input.files.length || this.uploading) {
          return;
        }
        const files = Array.from(this.input.files);
        this.input.value = '';
        this.queueUploads(files);
      });
      this.form.addEventListener('submit', (event) => {
        if (this.uploading) {
          event.preventDefault();
          this.setStatus('正在上传文件，请稍候完成后再提交。', 'warning');
        }
      });
    }

    async queueUploads(files) {
      if (!files.length) {
        return;
      }
      this.uploading = true;
      for (const file of files) {
        try {
          await this.uploadSingle(file);
        } catch (error) {
          console.error(error);
          this.setStatus(error.message || '上传失败，请重试。', 'danger');
          this.uploading = false;
          return;
        }
      }
      this.uploading = false;
      this.setStatus('文件已上传至 OSS，提交表单即可保存。', 'success');
    }

    async uploadSingle(file) {
      if (!file || !file.name) {
        throw new Error('无效的文件。');
      }
      if (config.max_size && file.size > config.max_size) {
        throw new Error(`单个文件大小不能超过 ${config.max_size_label || '限制值' }。`);
      }
      this.setStatus(`正在上传 ${file.name} ...`, 'info');
      const ticket = await this.requestTicket(file);
      await this.performUpload(file, ticket);
      this.recordUpload(ticket, file);
    }

    async requestTicket(file) {
      const response = await fetch(config.presign_url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify({
          filename: file.name,
          content_type: file.type || 'application/octet-stream',
          size: file.size
        })
      });
      if (!response.ok) {
        let detail;
        try {
          detail = await response.json();
        } catch (error) {
          detail = {};
        }
        if (detail && detail.error === 'file_too_large') {
          throw new Error(`文件超过限制（最大 ${config.max_size_label || '设定值'}）。`);
        }
        throw new Error('无法获取上传授权，请稍后再试。');
      }
      return response.json();
    }

    performUpload(file, ticket) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('PUT', ticket.upload_url);
        const headers = ticket.headers || {};
        Object.keys(headers).forEach((key) => {
          if (headers[key]) {
            xhr.setRequestHeader(key, headers[key]);
          }
        });
        xhr.upload.addEventListener('progress', (event) => {
          if (event.lengthComputable) {
            const percent = Math.round((event.loaded / event.total) * 100);
            this.setStatus(`正在上传 ${file.name}（${percent}%）`, 'info');
          }
        });
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve();
          } else {
            reject(new Error(`上传 ${file.name} 时 OSS 返回错误 ${xhr.status}`));
          }
        };
        xhr.onerror = () => reject(new Error(`上传 ${file.name} 时发生网络错误。`));
        xhr.send(file);
      });
    }

    recordUpload(ticket, file) {
      if (!ticket || !ticket.object_key) {
        return;
      }
      const entry = {
        key: ticket.object_key,
        name: file.name,
        size: file.size
      };
      this.entries.push(entry);
      const hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = this.remoteFieldName;
      hidden.value = entry.key;
      hidden.dataset.key = entry.key;
      this.hiddenContainer.appendChild(hidden);
      this.renderList();
    }

    renderList() {
      this.listEl.innerHTML = '';
      if (!this.entries.length) {
        return;
      }
      this.entries.forEach((entry) => {
        const chip = document.createElement('span');
        chip.className = 'badge rounded-pill text-bg-secondary me-2 mb-2 d-inline-flex align-items-center gap-2';
        chip.textContent = entry.name;
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'btn-close btn-close-white btn-sm ms-1';
        removeBtn.setAttribute('aria-label', '移除');
        removeBtn.addEventListener('click', () => this.removeEntry(entry.key));
        chip.appendChild(removeBtn);
        this.listEl.appendChild(chip);
      });
    }

    removeEntry(key) {
      this.entries = this.entries.filter((entry) => entry.key !== key);
      const hiddenInputs = Array.from(this.hiddenContainer.querySelectorAll('input'));
      hiddenInputs.forEach((node) => {
        if (node.dataset.key === key) {
          node.remove();
        }
      });
      this.renderList();
    }

    setStatus(message, tone) {
      if (!this.statusEl) {
        return;
      }
      const toneClasses = {
        success: 'text-success',
        danger: 'text-danger',
        warning: 'text-warning',
        info: 'text-info'
      };
      this.statusEl.className = 'form-text mt-1';
      this.statusEl.classList.add(toneClasses[tone] || 'text-muted');
      this.statusEl.textContent = message || '';
    }
  }

  function hydrateInputs() {
    const inputs = document.querySelectorAll('input[type="file"][data-direct-upload="oss"]');
    inputs.forEach((input) => {
      if (!input.__directUploader) {
        input.__directUploader = new DirectOssUploader(input);
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', hydrateInputs);
  } else {
    hydrateInputs();
  }
})();
