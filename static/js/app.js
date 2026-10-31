/* ================= АНТИЧИТ (БЛОКИРОВКА КЛАВИШ) ================= */
    document.addEventListener('keydown', (e) => {
        if (e.key === 'F12' || (e.ctrlKey && e.shiftKey && e.key === 'I') || (e.ctrlKey && e.key === 'c') || (e.ctrlKey && e.key === 'v') || (e.key === 'PrintScreen')) {
            e.preventDefault(); return false;
        }
    });
    document.addEventListener('keyup', (e) => {
        if (e.key === 'PrintScreen') {
            try { navigator.clipboard.writeText('').catch(() => {}); } catch (err) {}
            showToast('📸 Скриншоты запрещены!');
        }
    });

    function formatPhoneInput(input) {
        let val = input.value.replace(/\D/g, '').substring(0, 9);
        let formatted = '';
        if (val.length > 0) formatted += val.substring(0, 2);
        if (val.length > 2) formatted += ' ' + val.substring(2, 5);
        if (val.length > 5) formatted += ' ' + val.substring(5, 7);
        if (val.length > 7) formatted += ' ' + val.substring(7, 9);
        input.value = formatted;
    }

    /* ================= АНТИЧИТ (ОТСЛЕЖИВАНИЕ ВКЛАДОК) ================= */
    let isTestActive = false;
    let warningsCount = 0;
    let warningOverlayActive = false;

    // Используем visibilitychange вместо blur для исключения ложных банов на мобильных устройствах
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden' && isTestActive && !warningOverlayActive) {
            warningsCount++;
            if (warningsCount >= 3) {
                // Провал
                isTestActive = false;
                clearInterval(timerInterval);
                clearSid();
                document.getElementById('cheat-fail-screen').style.display = 'flex';
            } else {
                // Предупреждение
                warningOverlayActive = true;
                document.getElementById('warning-count').innerText = warningsCount;
                document.getElementById('warning-screen').style.display = 'flex';
            }
        }
    });

    function closeWarning() {
        warningOverlayActive = false;
        document.getElementById('warning-screen').style.display = 'none';
    }

    // Предупреждение перед закрытием/обновлением вкладки
    window.addEventListener('beforeunload', (e) => {
        if (isTestActive) {
            e.preventDefault();
            e.returnValue = 'Тест еще не завершен. Ваши ответы будут потеряны!';
            return e.returnValue;
        }
    });
    /* ================================================================ */

    // Экранирование HTML — защита от XSS при отрисовке вариантов ответа
    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
    }

    /* ================= СОСТОЯНИЕ ТЕСТА =================
       Вопросы выдаются сервером по одному (сессионный режим):
       клиент не знает правильных ответов и всего списка вопросов. */
    let sid = null;
    let currentQ = null;
    let totalQuestions = 50;
    let timerInterval;
    let timeLeft;
    let totalTime;
    let testStartTime;

    function initApp() {
        const startQTotal = document.getElementById('start-q-total');
        if (startQTotal) startQTotal.innerText = totalQuestions;

        // Подсветка выбранного филиала классом .selected (надежнее, чем :has())
        document.querySelectorAll('.branch-btn input').forEach((radio) => {
            radio.addEventListener('change', () => {
                document.querySelectorAll('.branch-btn').forEach((el) => el.classList.remove('selected'));
                radio.closest('.branch-btn').classList.add('selected');
            });
        });

        tryResume();
    }

    // Случайная перезагрузка (свайп на мобильном) не убивает тест:
    // sid хранится в sessionStorage, сессия живет на сервере
    function saveSid(id) {
        try { sessionStorage.setItem('slc_sid', id); } catch (e) {}
    }

    function clearSid() {
        try { sessionStorage.removeItem('slc_sid'); } catch (e) {}
    }

    async function tryResume() {
        let saved = null;
        try { saved = sessionStorage.getItem('slc_sid'); } catch (e) {}
        if (!saved) return;

        try {
            const res = await fetch('/api/question/' + saved);
            const data = await res.json();
            if (data.status !== 'success') {
                clearSid();
                return;
            }
            sid = saved;
            if (data.total) totalQuestions = data.total;
            document.getElementById('q-total').innerText = totalQuestions;
            isTestActive = true;
            testStartTime = Date.now();

            document.getElementById('start-screen').classList.remove('active');
            if (data.finished) {
                // Тест отвечен, но не отправлен — сразу к форме
                document.getElementById('progress-bar').style.width = '100%';
                document.getElementById('form-screen').classList.add('active');
            } else {
                document.getElementById('question-screen').classList.add('active');
                renderQuestion(data.question);
            }
            showToast('Продолжаем с того места, где вы остановились.');
        } catch (e) {
            // Сервер недоступен — обычный старт
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initApp);
    } else {
        initApp();
    }

    function startTest() {
        isTestActive = true;
        testStartTime = Date.now();
        swipeScreen('start-screen', 'question-screen');

        document.getElementById('question-text').innerText = 'Загрузка...';

        fetch('/test/start', { method: 'POST' })
            .then(async response => {
                const res = await response.json();
                if (!response.ok || res.status !== 'success') throw new Error(res.message || 'Ошибка запуска теста');
                return res;
            })
            .then(res => {
                sid = res.sid;
                saveSid(sid);
                totalQuestions = res.total;
                document.getElementById('q-total').innerText = totalQuestions;
                setTimeout(() => renderQuestion(res.question), 400);
            })
            .catch(err => {
                showToast('Ошибка: ' + err.message);
                isTestActive = false;
                // Возвращаем стартовый экран
                const q = document.getElementById('question-screen');
                const s = document.getElementById('start-screen');
                q.classList.remove('active');
                s.classList.add('active');
            });
    }

    function swipeScreen(hideId, showId) {
        const hideEl = document.getElementById(hideId);
        const showEl = document.getElementById(showId);
        hideEl.style.opacity = '0';
        hideEl.style.transform = 'translateX(-50px)';
        setTimeout(() => {
            hideEl.classList.remove('active');
            showEl.classList.add('active');
            showEl.style.opacity = '0';
            showEl.style.transform = 'translateX(50px)';
            void showEl.offsetWidth;
            showEl.style.opacity = '1';
            showEl.style.transform = 'translateX(0)';
        }, 300);
    }

    function renderQuestion(q) {
        currentQ = q;
        const questionText = document.getElementById('question-text');
        const container = document.getElementById('answer-container');

        document.getElementById('q-current').innerText = q.id;
        const progressPercent = ((q.id - 1) / totalQuestions) * 100;
        document.getElementById('progress-bar').style.width = progressPercent + '%';

        questionText.style.opacity = 0; container.style.opacity = 0;
        questionText.style.transform = 'translateX(20px)'; container.style.transform = 'translateX(20px)';

        setTimeout(() => {
            questionText.innerText = q.question;
            container.innerHTML = '';

            totalTime = q.time_limit; timeLeft = q.time_limit;
            if (q.type === 'choice') {
                const optionsDiv = document.createElement('div');
                optionsDiv.className = 'options';
                q.options.forEach((opt) => {
                    const label = document.createElement('label');
                    label.className = 'option-label';
                    label.innerHTML = `<input type="radio" name="answer" value="${escapeHtml(opt)}"> ${escapeHtml(opt)}`;
                    // Подсветка выбранного варианта классом .selected (надежнее, чем :has())
                    label.querySelector('input').addEventListener('change', () => {
                        optionsDiv.querySelectorAll('.option-label').forEach((el) => el.classList.remove('selected'));
                        label.classList.add('selected');
                    });
                    optionsDiv.appendChild(label);
                });
                container.appendChild(optionsDiv);
            } else if (q.type === 'input') {
                container.innerHTML = `<input type="text" id="open-answer" placeholder="Введите ответ..." onpaste="return false;" autocomplete="off" onkeydown="if(event.key==='Enter') nextQuestion();">`;
                setTimeout(() => document.getElementById('open-answer').focus(), 100);
            }

            questionText.style.opacity = 1; container.style.opacity = 1;
            questionText.style.transform = 'translateX(0)'; container.style.transform = 'translateX(0)';

            updateTimerDisplay();
            startTimer();
        }, 200);
    }

    function startTimer() {
        clearInterval(timerInterval);
        const timerBox = document.getElementById('timer-box');
        timerBox.classList.remove('urgent');

        timerInterval = setInterval(() => {
            timeLeft--;
            updateTimerDisplay();

            if (timeLeft <= 5 && timeLeft > 0) { timerBox.classList.add('urgent'); }
            if (timeLeft <= 0) {
                clearInterval(timerInterval);
                timerBox.classList.remove('urgent');
                nextQuestion(true);
            }
        }, 1000);
    }

    function updateTimerDisplay() {
        document.getElementById('time-left').innerText = timeLeft;
        const offset = 138 - (timeLeft / totalTime) * 138;
        document.getElementById('timer-path').style.strokeDashoffset = offset;
    }

    let isTransitioning = false;

    let toastTimeout;
    function showToast(message) {
        const toast = document.getElementById('custom-toast');
        const toastText = document.getElementById('toast-text');
        toastText.innerText = message;
        toast.classList.add('show');

        clearTimeout(toastTimeout);
        toastTimeout = setTimeout(() => {
            toast.classList.remove('show');
        }, 3000);
    }

    function nextQuestion(timeOut = false) {
        if (isTransitioning || !currentQ) return;

        let answerVal = '';
        if (currentQ.type === 'choice') {
            const selected = document.querySelector('input[name="answer"]:checked');
            if (selected) answerVal = selected.value;
        } else if (currentQ.type === 'input') {
            const inputEl = document.getElementById('open-answer');
            if (inputEl) {
                answerVal = inputEl.value.trim();
                inputEl.blur(); // Скрываем мобильную клавиатуру
            }
        }

        // Если время не вышло и ответ не выбран - требуем ответ
        if (!timeOut && !answerVal) {
            const container = document.querySelector('.container');
            container.classList.remove('shake');
            void container.offsetWidth; // Триггер рефлоу для повторной анимации
            container.classList.add('shake');

            showToast("Пожалуйста, выберите или введите вариант ответа перед переходом.");
            return;
        }

        clearInterval(timerInterval);
        isTransitioning = true;

        // Блокируем кнопку до ответа сервера — защита от двойной отправки
        const nextBtn = document.getElementById('next-btn');
        nextBtn.disabled = true;

        // Ответ уходит на сервер, в ответе сразу приходит следующий вопрос
        fetch('/api/answer/' + sid, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pos: currentQ.id, answer: answerVal })
        })
        .then(async response => {
            const res = await response.json();
            if (!response.ok || res.status !== 'success') throw new Error(res.message || 'Ошибка сервера');
            return res;
        })
        .then(res => {
            nextBtn.disabled = false;
            if (res.next) {
                renderQuestion(res.next);
            } else {
                endTest();
            }
        })
        .catch(() => {
            nextBtn.disabled = false;
            // Сеть подвела: пробуем ресинхронизироваться с сервером
            showToast('Проблема соединения. Восстанавливаем...');
            resyncAfterError();
        });

        setTimeout(() => { isTransitioning = false; }, 350);
    }

    async function resyncAfterError() {
        try {
            const res = await fetch('/api/question/' + sid);
            const data = await res.json();
            if (data.status === 'success') {
                if (data.finished) { endTest(); return; }
                renderQuestion(data.question);
                return;
            }
        } catch (e) { /* сеть недоступна */ }
        startTimer(); // не смогли — продолжаем текущий вопрос с полным таймером
    }

    function endTest() {
        isTestActive = false; // Отключаем античит
        clearInterval(timerInterval);
        document.getElementById('progress-bar').style.width = '100%';
        swipeScreen('question-screen', 'form-screen');
    }

    function submitResults() {
        const fio = document.getElementById('fio').value.trim();
        const phoneRaw = document.getElementById('phone').value.replace(/\D/g, '');
        const branchInput = document.querySelector('input[name="branch"]:checked');

        const container = document.querySelector('.container');
        const triggerShake = () => {
            container.classList.remove('shake');
            void container.offsetWidth;
            container.classList.add('shake');
        };

        if (!fio || fio.length < 3) {
            triggerShake();
            showToast("Пожалуйста, введите корректную Фамилию и Имя.");
            document.getElementById('fio').focus();
            return;
        }
        if (phoneRaw.length !== 9) {
            triggerShake();
            showToast("Номер телефона должен содержать 9 цифр после +998.");
            document.getElementById('phone').focus();
            return;
        }
        if (!branchInput) {
            triggerShake();
            showToast("Пожалуйста, выберите филиал.");
            return;
        }

        const elapsed = Math.round((Date.now() - testStartTime) / 1000);
        const mins = Math.floor(elapsed / 60);
        const secs = elapsed % 60;
        const timeStr = mins + ' мин ' + secs + ' сек';

        // Ответы не отправляются: они уже хранятся в серверной сессии
        const data = {
            sid: sid,
            fio: fio,
            phone: "+998" + phoneRaw,
            branch: branchInput.value,
            time: timeStr,
            hp: (document.getElementById('hp-field')?.value || '')
        };

        const btn = document.querySelector('#form-screen .btn');
        btn.innerText = 'Отправка...'; btn.disabled = true;

        fetch('/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })
        .then(async response => {
            const res = await response.json();
            if (!response.ok || res.status !== 'success') {
                throw new Error(res.message || 'Ошибка сервера при отправке');
            }
            return res;
        })
        .then(res => {
            swipeScreen('form-screen', 'success-screen');
            clearSid();
            setTimeout(() => {
                // Конфетти — только если CDN загрузился и пользователь не просил меньше анимаций
                if (window.confetti && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
                    confetti({ particleCount: 150, spread: 70, origin: { y: 0.6 }, colors: ['#FFB703', '#070707', '#ffffff'] });
                }
            }, 400);
        })
        .catch(error => {
            console.error('Error:', error);
            triggerShake();
            showToast("Ошибка: " + error.message);
            btn.innerText = 'Отправить результаты'; btn.disabled = false;
        });
    }
