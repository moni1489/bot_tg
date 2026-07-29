const tg = window.Telegram?.WebApp;
if (tg) {
    tg.expand();
    tg.ready();
    tg.setHeaderColor('#000000');
    tg.setBackgroundColor('#000000');
}

// Get user ID from Telegram WebApp, URL params, or generate unique localStorage ID
function getEffectiveUserId() {
    if (tg?.initDataUnsafe?.user?.id) {
        return tg.initDataUnsafe.user.id;
    }
    const urlParams = new URLSearchParams(window.location.search);
    const paramId = urlParams.get('tg_id');
    if (paramId) {
        return parseInt(paramId, 10);
    }
    let localId = localStorage.getItem('funko_cards_uid');
    if (!localId) {
        localId = Math.floor(Math.random() * 89999999) + 10000000;
        localStorage.setItem('funko_cards_uid', localId);
    }
    return parseInt(localId, 10);
}

const effectiveTgId = getEffectiveUserId();

// User state
let userData = {
    telegram_id: effectiveTgId,
    first_name: tg?.initDataUnsafe?.user?.first_name || 'Игрок',
    username: tg?.initDataUnsafe?.user?.username || 'player',
    packs_count: 5,
    last_daily_pack: null,
    completed_tasks: [],
    ref_code: 'ref_' + effectiveTgId
};

let userCards = {}; 
let isOpening = false;
let dailyTimerInterval = null;

// 7 Series Configurations (28 total cards)
const SERIES_CONFIG = [
    {
        slug: 'breaking_bad',
        name: 'Breaking Bad',
        theme: 'breaking_bad',
        cards: [
            { index: 1, name: 'Walter White', rarity: 'legendary', img: '/cards/images/card_breaking_bad_1.png' },
            { index: 2, name: 'Jesse Pinkman', rarity: 'epic', img: '/cards/images/card_breaking_bad_2.png' },
            { index: 3, name: 'Saul Goodman', rarity: 'rare', img: '/cards/images/card_breaking_bad_3.png' },
            { index: 4, name: 'Gus Fring', rarity: 'common', img: '/cards/images/card_breaking_bad_4.png' }
        ]
    },
    {
        slug: 'marvel',
        name: 'Marvel',
        theme: 'marvel',
        cards: [
            { index: 1, name: 'Iron Man', rarity: 'legendary', img: '/cards/images/card_marvel_1.png' },
            { index: 2, name: 'Spider-Man', rarity: 'epic', img: '/cards/images/card_marvel_2.png' },
            { index: 3, name: 'Captain America', rarity: 'rare', img: '/cards/images/card_marvel_3.png' },
            { index: 4, name: 'Deadpool', rarity: 'common', img: '/cards/images/card_marvel_4.png' }
        ]
    },
    {
        slug: 'dc',
        name: 'DC Comics',
        theme: 'dc',
        cards: [
            { index: 1, name: 'Batman', rarity: 'legendary', img: '/cards/images/card_dc_1.png' },
            { index: 2, name: 'Superman', rarity: 'epic', img: '/cards/images/card_dc_2.png' },
            { index: 3, name: 'Joker', rarity: 'rare', img: '/cards/images/card_dc_3.png' },
            { index: 4, name: 'Flash', rarity: 'common', img: '/cards/images/card_dc_4.png' }
        ]
    },
    {
        slug: 'death_note',
        name: 'Death Note',
        theme: 'death_note',
        cards: [
            { index: 1, name: 'Ryuk', rarity: 'legendary', img: '/cards/images/card_death_note_1.png' },
            { index: 2, name: 'L', rarity: 'epic', img: '/cards/images/card_death_note_2.png' },
            { index: 3, name: 'Light Yagami', rarity: 'rare', img: '/cards/images/card_death_note_3.png' },
            { index: 4, name: 'Misa Aane', rarity: 'common', img: '/cards/images/card_death_note_4.png' }
        ]
    },
    {
        slug: 'invincible',
        name: 'Invincible',
        theme: 'invincible',
        cards: [
            { index: 1, name: 'Omni-Man', rarity: 'legendary', img: '/cards/images/card_invincible_1.png' },
            { index: 2, name: 'Invincible', rarity: 'epic', img: '/cards/images/card_invincible_2.png' },
            { index: 3, name: 'Atom Eve', rarity: 'rare', img: '/cards/images/card_invincible_3.png' },
            { index: 4, name: 'Allen', rarity: 'common', img: '/cards/images/card_invincible_4.png' }
        ]
    },
    {
        slug: 'one_piece',
        name: 'One Piece',
        theme: 'one_piece',
        cards: [
            { index: 1, name: 'Luffy', rarity: 'legendary', img: '/cards/images/card_one_piece_1.png' },
            { index: 2, name: 'Zoro', rarity: 'epic', img: '/cards/images/card_one_piece_2.png' },
            { index: 3, name: 'Sanji', rarity: 'rare', img: '/cards/images/card_one_piece_3.png' },
            { index: 4, name: 'Nami', rarity: 'common', img: '/cards/images/card_one_piece_4.png' }
        ]
    },
    {
        slug: 'universal',
        name: 'Universal',
        theme: 'universal',
        cards: [
            { index: 1, name: 'Funko Gold Crown', rarity: 'legendary', img: '/cards/images/card_universal_1.png' },
            { index: 2, name: 'Funko Silver', rarity: 'epic', img: '/cards/images/card_universal_2.png' },
            { index: 3, name: 'Funko Bronze', rarity: 'rare', img: '/cards/images/card_universal_3.png' },
            { index: 4, name: 'Funko Classic', rarity: 'common', img: '/cards/images/card_universal_4.png' }
        ]
    }
];

// DOM Elements
const openPackBtn = document.getElementById('open-pack-btn');
const packsCountBadge = document.getElementById('packs-count-badge');
const boosterPack = document.getElementById('booster-pack');
const cardStage = document.getElementById('card-stage');
const card3d = document.getElementById('card-3d');
const cardFront = document.getElementById('card-front');
const seriesContainer = document.getElementById('series-container');
const totalProgressText = document.getElementById('total-progress-text');
const refLinkInput = document.getElementById('ref-link-input');
const copyRefBtn = document.getElementById('copy-ref-btn');
const dailyGiftBtn = document.getElementById('daily-gift-btn');
const dailyTimer = document.getElementById('daily-timer');

// Init
async function initApp() {
    setupNavigation();
    setupRefLink();
    setupDailyPackButton();
    setupTasks();
    await fetchProfile();
    renderCollection();
    updateUI();
}

function updateUI() {
    packsCountBadge.textContent = userData.packs_count;
    if (userData.packs_count <= 0) {
        openPackBtn.style.opacity = '0.6';
    } else {
        openPackBtn.style.opacity = '1';
    }
    updateTaskButtons();
}

function setupNavigation() {
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-view').forEach(v => {
                v.classList.add('hidden');
                v.classList.remove('active');
            });
            
            btn.classList.add('active');
            const targetId = btn.getAttribute('data-target');
            const targetView = document.getElementById(targetId);
            if (targetView) {
                targetView.classList.remove('hidden');
                targetView.classList.add('active');
            }

            // Hide floating daily gift button on Collection & Tasks tabs
            const dailyBtn = document.getElementById('daily-gift-btn');
            if (dailyBtn) {
                if (targetId === 'view-home') {
                    dailyBtn.classList.remove('hidden');
                } else {
                    dailyBtn.classList.add('hidden');
                }
            }
        });
    });
}

let botUsername = "funkostop_bot";

function setupRefLink() {
    const link = `https://t.me/${botUsername}?start=ref_${userData.telegram_id}`;
    if (refLinkInput) refLinkInput.value = link;
    if (copyRefBtn) {
        copyRefBtn.onclick = () => {
            navigator.clipboard.writeText(refLinkInput.value);
            copyRefBtn.textContent = 'Скопировано';
            setTimeout(() => copyRefBtn.textContent = 'Копировать', 2000);
        };
    }
}

// API Calls
async function fetchProfile() {
    try {
        const res = await fetch(`/api/cards/profile?tg_id=${userData.telegram_id}`);
        if (res.ok) {
            const data = await res.json();
            userData.packs_count = data.packs_count;
            userData.last_daily_pack = data.last_daily_pack;
            userCards = data.user_cards || {};
            userData.completed_tasks = data.completed_tasks || [];
            userData.ref_count = data.ref_count || 0;
            if (data.bot_username) {
                botUsername = data.bot_username;
                setupRefLink();
            }
            checkDailyTimer();
            updateUI();
        }
    } catch (e) {
        console.log("Using default profile");
        checkDailyTimer();
    }
}

// Tasks System
function setupTasks() {
    document.querySelectorAll('.btn-task[data-task]').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (btn.classList.contains('completed')) return;
            
            const taskId = btn.getAttribute('data-task');
            
            // Open official Telegram channel if tg_sub task
            if (taskId === 'tg_sub') {
                window.open('https://t.me/FunkoStop', '_blank');
            }
            
            try {
                const res = await fetch('/api/cards/tasks/claim', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: userData.telegram_id, task_id: taskId })
                });
                const data = await res.json();
                
                if (data.success) {
                    userData.packs_count = data.packs_count;
                    userData.completed_tasks = data.completed_tasks;
                    updateUI();
                    
                    if (window.confetti) {
                        confetti({ particleCount: 50, spread: 60, origin: { y: 0.8 } });
                    }
                } else {
                    alert(data.message || "Не удалось выполнить задание.");
                }
            } catch (e) {
                console.error("Task claim error", e);
                alert("Ошибка сети или серверов. Попробуйте еще раз.");
            }
        });
    });
}

function updateTaskButtons() {
    document.querySelectorAll('.btn-task[data-task]').forEach(btn => {
        const taskId = btn.getAttribute('data-task');
        if (userData.completed_tasks.includes(taskId)) {
            btn.textContent = 'ВЫПОЛНЕНО';
            btn.classList.add('completed');
        }
    });

    // Update referral counter tag
    const refCountTag = document.getElementById('ref-count-tag');
    if (refCountTag) {
        refCountTag.textContent = `Приглашено друзей: ${userData.ref_count || 0}`;
    }
}

// Daily Pack Claiming
function setupDailyPackButton() {
    if (!dailyGiftBtn) return;
    
    dailyGiftBtn.addEventListener('click', async () => {
        const isReady = dailyTimer.textContent === "ГОТОВО";
        if (!isReady) {
            // Navigate to tasks tab when clicked and not ready
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-view').forEach(v => v.classList.add('hidden'));
            
            const tasksNavBtn = document.querySelector('.nav-btn[data-target="view-community"]');
            if (tasksNavBtn) tasksNavBtn.classList.add('active');
            document.getElementById('view-community').classList.remove('hidden');
            dailyGiftBtn.classList.add('hidden');
            return;
        }

        try {
            const res = await fetch('/api/cards/claim_daily', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ telegram_id: userData.telegram_id })
            });
            const data = await res.json();
            if (data.success) {
                userData.packs_count = data.packs_count;
                userData.last_daily_pack = new Date().toISOString();
                updateUI();
                startDailyCountdown(86400);
                
                if (window.confetti) {
                    confetti({ particleCount: 100, spread: 70, origin: { y: 0.5 } });
                }
            } else {
                if (data.seconds_left) {
                    startDailyCountdown(data.seconds_left);
                } else {
                    alert(data.message || "Ежедневный пак пока недоступен.");
                }
            }
        } catch (e) {
            console.error("Daily pack claim fallback", e);
            userData.packs_count++;
            userData.last_daily_pack = new Date().toISOString();
            updateUI();
            startDailyCountdown(86400);
        }
    });
}

function checkDailyTimer() {
    if (!dailyTimer) return;
    if (!userData.last_daily_pack) {
        dailyTimer.textContent = "ГОТОВО";
        dailyTimer.style.color = "#00ff88";
        dailyTimer.style.borderColor = "#00ff88";
        return;
    }

    const last = new Date(userData.last_daily_pack).getTime();
    const now = new Date().getTime();
    const diffSeconds = Math.floor((now - last) / 1000);

    if (isNaN(diffSeconds) || diffSeconds >= 86400) {
        dailyTimer.textContent = "ГОТОВО";
        dailyTimer.style.color = "#00ff88";
        dailyTimer.style.borderColor = "#00ff88";
    } else {
        startDailyCountdown(86400 - diffSeconds);
    }
}

function startDailyCountdown(secondsLeft) {
    if (dailyTimerInterval) clearInterval(dailyTimerInterval);

    dailyTimer.style.color = "#ff0022";
    dailyTimer.style.borderColor = "#ff0022";

    function tick() {
        if (secondsLeft <= 0) {
            clearInterval(dailyTimerInterval);
            dailyTimer.textContent = "ГОТОВО";
            dailyTimer.style.color = "#00ff88";
            dailyTimer.style.borderColor = "#00ff88";
            return;
        }
        const h = Math.floor(secondsLeft / 3600).toString().padStart(2, '0');
        const m = Math.floor((secondsLeft % 3600) / 60).toString().padStart(2, '0');
        const s = (secondsLeft % 60).toString().padStart(2, '0');
        dailyTimer.textContent = `${h}:${m}:${s}`;
        secondsLeft--;
    }

    tick();
    dailyTimerInterval = setInterval(tick, 1000);
}

// API Calls
async function fetchProfile() {
    try {
        const res = await fetch(`/api/cards/profile?tg_id=${userData.telegram_id}`);
        if (res.ok) {
            const data = await res.json();
            userData.packs_count = data.packs_count;
            userData.last_daily_pack = data.last_daily_pack;
            userCards = data.user_cards || {};
            userData.completed_tasks = data.completed_tasks || [];
            checkDailyTimer();
            updateUI();
        }
    } catch (e) {
        console.log("Using default profile");
        checkDailyTimer();
    }
}

// Roll Card
function rollRandomCard() {
    const rand = Math.random() * 100;
    let selectedRarity = 'common';
    if (rand <= 3) selectedRarity = 'legendary';
    else if (rand <= 15) selectedRarity = 'epic';
    else if (rand <= 40) selectedRarity = 'rare';
    else selectedRarity = 'common';

    const matching = [];
    SERIES_CONFIG.forEach(s => {
        s.cards.forEach(c => {
            if (c.rarity === selectedRarity) {
                matching.push({ series: s, card: c });
            }
        });
    });

    return matching[Math.floor(Math.random() * matching.length)];
}

// Pack Opening Flow
openPackBtn.addEventListener('click', async () => {
    if (isOpening) return;
    if (userData.packs_count <= 0) {
        alert("У вас закончились паки! Заберите ежедневный пак или выполняйте задания.");
        return;
    }

    isOpening = true;
    userData.packs_count--;
    updateUI();

    boosterPack.classList.add('shaking');
    cardStage.classList.add('hidden');
    card3d.classList.remove('flipped', 'aura-epic', 'aura-legendary');

    setTimeout(() => {
        boosterPack.classList.remove('shaking');
        boosterPack.classList.add('hidden');
        cardStage.classList.remove('hidden');

        const drop = rollRandomCard();
        const cardKey = `${drop.series.slug}_${drop.card.index}`;
        userCards[cardKey] = (userCards[cardKey] || 0) + 1;

        cardFront.innerHTML = `
            <div class="card-frame ${drop.series.theme}">
                <div class="card-series-tag">${drop.series.name}</div>
                <img src="${drop.card.img}" class="card-character-image" alt="${drop.card.name}" onerror="this.style.opacity='0.1';">
                <div class="card-character-name">${drop.card.name}</div>
                <div class="card-rarity-badge rarity-${drop.card.rarity}">${drop.card.rarity}</div>
            </div>
        `;

        // Apply Glowing Aura for Epic & Legendary (as in Shorts reference)
        if (drop.card.rarity === 'epic') {
            card3d.classList.add('aura-epic');
        } else if (drop.card.rarity === 'legendary') {
            card3d.classList.add('aura-legendary');
        }

        setTimeout(() => {
            card3d.classList.add('flipped');

            if (window.confetti) {
                confetti({
                    particleCount: drop.card.rarity === 'legendary' ? 150 : (drop.card.rarity === 'epic' ? 80 : 40),
                    spread: drop.card.rarity === 'legendary' ? 100 : 70,
                    origin: { y: 0.6 }
                });
            }

            syncOpenedCard(drop.series.slug, drop.card.index);

            setTimeout(() => {
                isOpening = false;
                boosterPack.classList.remove('hidden');
                cardStage.classList.add('hidden');
                card3d.classList.remove('flipped');
                renderCollection();
            }, 3000);

        }, 400);

    }, 1200);
});

async function syncOpenedCard(seriesSlug, cardIndex) {
    try {
        await fetch('/api/cards/open', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                telegram_id: userData.telegram_id,
                series_slug: seriesSlug,
                card_index: cardIndex
            })
        });
    } catch (e) {
        console.log("Card opened offline/simulated");
    }
}

// Render Collection
function renderCollection() {
    seriesContainer.innerHTML = '';
    let totalCollected = 0;
    
    // Calculate total first
    SERIES_CONFIG.forEach(series => {
        series.cards.forEach(card => {
            const cardKey = `${series.slug}_${card.index}`;
            if (userCards[cardKey] > 0) totalCollected++;
        });
    });

    // Update global header stats
    const totalCards = SERIES_CONFIG.length * 4;
    const missing = totalCards - totalCollected;
    let totalDupes = 0;
    Object.values(userCards).forEach(count => {
        if (count > 1) totalDupes += (count - 1);
    });

    // We can inject stats right above the container
    const statsHtml = `
        <div class="collection-stats">
            <div class="stat-box">
                <span class="stat-val">${totalCards}</span>
                <span class="stat-label">ВСЕГО</span>
            </div>
            <div class="stat-box">
                <span class="stat-val" style="color:#00ff88;">${totalCollected}</span>
                <span class="stat-label">СОБРАНО</span>
            </div>
            <div class="stat-box">
                <span class="stat-val" style="color:var(--neon-red);">${missing}</span>
                <span class="stat-label">ОТСУТСТВУЕТ</span>
            </div>
            <div class="stat-box">
                <span class="stat-val">${totalDupes}</span>
                <span class="stat-label">ДУБЛИКАТЫ</span>
            </div>
        </div>
        <br>
    `;
    seriesContainer.innerHTML = statsHtml;

    SERIES_CONFIG.forEach(series => {
        let seriesCollectedCount = 0;
        
        const seriesBlock = document.createElement('div');
        seriesBlock.className = 'series-card-block';

        const titleRow = document.createElement('div');
        titleRow.className = 'series-title-row';

        const listView = document.createElement('div');
        listView.className = 'cards-list-view';

        series.cards.forEach(card => {
            const cardKey = `${series.slug}_${card.index}`;
            const count = userCards[cardKey] || 0;
            const isCollected = count > 0;

            if (isCollected) {
                seriesCollectedCount++;
            }

            const listItem = document.createElement('div');
            listItem.className = 'card-list-item';
            
            const leftCol = document.createElement('div');
            leftCol.className = 'card-item-left';
            leftCol.innerHTML = `
                <span class="card-check">${isCollected ? '✅' : '❌'}</span>
                <span class="card-item-name" style="color: ${isCollected ? '#fff' : '#666'}">${card.name}</span>
            `;

            const rightCol = document.createElement('div');
            rightCol.className = `card-item-right ${count > 1 ? 'has-dupes' : ''}`;
            rightCol.textContent = isCollected ? `${count} шт.` : '0 шт.';

            listItem.appendChild(leftCol);
            listItem.appendChild(rightCol);
            listView.appendChild(listItem);
        });

        titleRow.innerHTML = `
            <div class="series-name">${series.name}</div>
            <div class="series-progress">${seriesCollectedCount} / 4</div>
        `;

        seriesBlock.appendChild(titleRow);
        seriesBlock.appendChild(listView);
        seriesContainer.appendChild(seriesBlock);
    });

    totalProgressText.textContent = ``;
}

initApp();
