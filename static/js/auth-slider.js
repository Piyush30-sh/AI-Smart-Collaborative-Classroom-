/* Double slider auth interactions and client-side validation. */
(function initAuthSlider() {
  const slider = document.getElementById('authSlider');
  if (!slider) return;

  document.querySelectorAll('.slider-server-message').forEach((message) => {
    window.setTimeout(() => {
      message.classList.add('is-dismissed');
      window.setTimeout(() => message.remove(), 240);
    }, 2000);
  });

  const fadeMessage = (message) => {
    message.classList.remove('is-visible');
    message.classList.add('is-dismissed');
    message.textContent = '';
  };
  const resetFormScroll = (selector) => {
    const form = slider.querySelector(selector);
    if (form) form.scrollTop = 0;
  };
  const showSignup = () => {
    slider.classList.add('slider-auth--signup-active');
    resetFormScroll('.slider-form--signup');
  };
  const showSignin = () => {
    slider.classList.remove('slider-auth--signup-active');
    resetFormScroll('.slider-form--signin');
  };

  resetFormScroll(slider.classList.contains('slider-auth--signup-active') ? '.slider-form--signup' : '.slider-form--signin');

  document.querySelectorAll('[data-show-signup]').forEach((button) => button.addEventListener('click', showSignup));
  document.querySelectorAll('[data-show-signin]').forEach((button) => button.addEventListener('click', showSignin));

  const signupForm = document.querySelector('[data-auth-form="signup"]');
  const roleInput = signupForm?.querySelector('input[name="user_type"]');
  const roleSwitch = document.querySelector('.slider-role-switch');
  document.querySelectorAll('.slider-role-switch [data-role]').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.slider-role-switch [data-role]').forEach((option) => option.classList.remove('is-active'));
      button.classList.add('is-active');
      if (roleInput) roleInput.value = button.dataset.role;
      if (roleSwitch) roleSwitch.dataset.activeRole = button.dataset.role;
    });
  });
  if (roleSwitch) roleSwitch.dataset.activeRole = 'student';

  document.querySelectorAll('.slider-social button').forEach((button) => {
    const release = () => button.classList.remove('is-pressed');
    button.addEventListener('pointerdown', () => button.classList.add('is-pressed'));
    button.addEventListener('pointerup', release);
    button.addEventListener('pointerleave', release);
    button.addEventListener('blur', release);
  });

  document.querySelectorAll('.slider-primary-button, .slider-outline-button').forEach((button) => {
    const release = () => button.classList.remove('is-pressed');
    button.addEventListener('pointerdown', () => button.classList.add('is-pressed'));
    button.addEventListener('pointerup', release);
    button.addEventListener('pointerleave', release);
    button.addEventListener('blur', release);
  });

  document.querySelectorAll('.slider-password-toggle').forEach((button) => {
    button.addEventListener('click', () => {
      const input = button.parentElement.querySelector('input');
      const icon = button.querySelector('i');
      const showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      button.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
      icon.classList.toggle('fa-eye', showing);
      icon.classList.toggle('fa-eye-slash', !showing);
    });
  });

  const signupPassword = document.querySelector('[data-auth-form="signup"] input[name="password"]');
  const strengthFill = document.querySelector('.slider-strength__track span');
  const strengthLabel = document.querySelector('[data-strength-label]');
  if (signupPassword && strengthFill && strengthLabel) {
    signupPassword.addEventListener('input', () => {
      const value = signupPassword.value;
      let score = 0;
      if (value.length >= 8) score += 1;
      if (value.length >= 12) score += 1;
      if (/[A-Z]/.test(value)) score += 1;
      if (/[0-9]/.test(value)) score += 1;
      if (/[^A-Za-z0-9]/.test(value)) score += 1;

      const levels = [
        { width: '0%', color: '#cbd5e1', label: 'Password strength' },
        { width: '20%', color: '#ef4444', label: 'Very weak' },
        { width: '40%', color: '#f97316', label: 'Weak' },
        { width: '60%', color: '#eab308', label: 'Fair' },
        { width: '80%', color: '#22c55e', label: 'Strong' },
        { width: '100%', color: '#16a34a', label: 'Very strong' },
      ];
      const level = levels[score];
      strengthFill.style.width = level.width;
      strengthFill.style.background = level.color;
      strengthLabel.textContent = level.label;
    });
  }

  document.querySelectorAll('[data-auth-form]').forEach((form) => {
    const error = form.querySelector('.slider-error');
    if (error) document.body.appendChild(error);
    form.addEventListener('submit', (event) => {
      const email = form.querySelector('input[name="email"]');
      const password = form.querySelector('input[name="password"]');
      const emailValid = email && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.value.trim());
      const passwordValid = password && password.value.length >= 8;
      const missingDetails = [];
      if (!email.value.trim()) missingDetails.push('email');
      else if (!emailValid) missingDetails.push('a valid email address');
      if (!password.value) missingDetails.push('password');
      else if (!passwordValid) missingDetails.push('8+ character password');
      let valid = emailValid && passwordValid;

      if (form.dataset.authForm === 'signup') {
        const firstName = form.querySelector('[data-first-name]');
        const lastName = form.querySelector('[data-last-name]');
        const confirm = form.querySelector('[data-confirm-password]');
        const terms = form.querySelector('.slider-terms input');
        const fullName = form.querySelector('[data-full-name]');
        if (!firstName.value.trim()) missingDetails.push('first name');
        if (!lastName.value.trim()) missingDetails.push('last name');
        if (!confirm.value) missingDetails.push('confirm password');
        else if (confirm.value !== password.value) missingDetails.push('matching passwords');
        const detailsValid = valid && firstName.value.trim() && lastName.value.trim() && confirm.value === password.value;
        valid = detailsValid && terms.checked;
        if (fullName) fullName.value = `${firstName.value.trim()} ${lastName.value.trim()}`.trim();

        if (detailsValid && !terms.checked) {
          missingDetails.length = 0;
           missingDetails.push('accept the terms & conditions');
        }
      }

      if (!valid) {
        event.preventDefault();
        const details = missingDetails.slice(0, 2);
        if(details.length === 1 && details[0] === 'accept the terms & conditions') {
          error.textContent = 'Please accept the terms & conditions.';
        }
        else{
        error.textContent = missingDetails.length > 2
          ? 'Complete the required fields.'
          : `Please enter ${details.join(' and ')}.`;
        }
        error.classList.remove('is-dismissed');
        error.classList.add('is-visible');
        window.clearTimeout(form.validationTimer);
        form.validationTimer = window.setTimeout(() => fadeMessage(error), 2000);
        return;
      }

      window.clearTimeout(form.validationTimer);
      fadeMessage(error);
      error.classList.remove('is-visible');
    });

    form.addEventListener('input', () => {
      window.clearTimeout(form.validationTimer);
      fadeMessage(error);
    });
  });
})();
