const tg = window.Telegram?.WebApp;
if (tg) {
    tg.expand();
    tg.ready();
    tg.setHeaderColor('#000000');
    tg.setBackgroundColor('#000000');
}

// User state
let userData = {
    telegram_id: tg?.initDataUnsafe?.user?.id || 12345678,
    first_name: tg?.initDataUnsafe?.user?.first_name || 'Игрок',
    username: tg?.initDataUnsafe?.user?.username || 'player',
    packs_count: 5,
    last_daily_pack: null,
    completed_tasks: [],
    ref_code: 'ref_' + (tg?.initDataUnsafe?.user?.id || 12345678)
};

let userCards = {}; 
let isOpening = false;
let dailyTimerInterval = null;

// 7 Series Configurations
const SERIES_CONFIG = [
    {
        slug: 'breaking_bad',
        name: 'Breaking Bad',
        theme: 'breaking_bad',
        cards: [
            { index: 1, name: 'Walter White', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1534447677768-be436bb09401?w=300' },
            { index: 2, name: 'Jesse Pinkman', rarity: 'epic', img: 'https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=300' },
            { index: 3, name: 'Saul Goodman', rarity: 'rare', img: 'https://images.unsplash.com/photo-1500648767791-00dcc994a43e?w=300' },
            { index: 4, name: 'Gus Fring', rarity: 'common', img: 'https://images.unsplash.com/photo-1472099645785-5658abf4ff4e?w=300' }
        ]
    },
    {
        slug: 'marvel',
        name: 'Marvel',
        theme: 'marvel',
        cards: [
            { index: 1, name: 'Iron Man', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1635863138275-d9b33299680b?w=300' },
            { index: 2, name: 'Spider-Man', rarity: 'epic', img: 'https://images.unsplash.com/photo-1604200213928-ba3cf4fc8436?w=300' },
            { index: 3, name: 'Captain America', rarity: 'rare', img: 'https://images.unsplash.com/photo-1569003339405-ea396a5a8a90?w=300' },
            { index: 4, name: 'Deadpool', rarity: 'common', img: 'https://images.unsplash.com/photo-1534809027769-b00d750a6bac?w=300' }
        ]
    },
    {
        slug: 'dc',
        name: 'DC Comics',
        theme: 'dc',
        cards: [
            { index: 1, name: 'Batman', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1509198397868-475647b2a1e5?w=300' },
            { index: 2, name: 'Superman', rarity: 'epic', img: 'https://images.unsplash.com/photo-1568602471122-7832951cc4c5?w=300' },
            { index: 3, name: 'Joker', rarity: 'rare', img: 'https://images.unsplash.com/photo-1579783902614-a3fb3927b675?w=300' },
            { index: 4, name: 'Flash', rarity: 'common', img: 'https://images.unsplash.com/photo-1519085360753-af0119f7cbe7?w=300' }
        ]
    },
    {
        slug: 'death_note',
        name: 'Death Note',
        theme: 'death_note',
        cards: [
            { index: 1, name: 'Ryuk', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1518709268805-4e9042af9f23?w=300' },
            { index: 2, name: 'L', rarity: 'epic', img: 'https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?w=300' },
            { index: 3, name: 'Light Yagami', rarity: 'rare', img: 'https://images.unsplash.com/photo-1501196354995-cbb51c65aaea?w=300' },
            { index: 4, name: 'Misa Aane', rarity: 'common', img: 'https://images.unsplash.com/photo-1524504388940-b1c1722653e1?w=300' }
        ]
    },
    {
        slug: 'invincible',
        name: 'Invincible',
        theme: 'invincible',
        cards: [
            { index: 1, name: 'Omni-Man', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1506794778202-cad84cf45f1d?w=300' },
            { index: 2, name: 'Invincible', rarity: 'epic', img: 'https://images.unsplash.com/photo-1517841905240-472988babdf9?w=300' },
            { index: 3, name: 'Atom Eve', rarity: 'rare', img: 'https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=300' },
            { index: 4, name: 'Allen', rarity: 'common', img: 'https://images.unsplash.com/photo-1522075469751-3a6694fb2f61?w=300' }
        ]
    },
    {
        slug: 'one_piece',
        name: 'One Piece',
        theme: 'one_piece',
        cards: [
            { index: 1, name: 'Luffy', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1544005313-94ddf0286df2?w=300' },
            { index: 2, name: 'Zoro', rarity: 'epic', img: 'https://images.unsplash.com/photo-1506794778202-cad84cf45f1d?w=300' },
            { index: 3, name: 'Sanji', rarity: 'rare', img: 'https://images.unsplash.com/photo-1519085360753-af0119f7cbe7?w=300' },
            { index: 4, name: 'Nami', rarity: 'common', img: 'https://images.unsplash.com/photo-1517841905240-472988babdf9?w=300' }
        ]
    },
    {
        slug: 'universal',
        name: 'Universal',
        theme: 'universal',
        cards: [
            { index: 1, name: 'Funko Gold Crown', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=300' },
            { index: 2, name: 'Funko Silver', rarity: 'epic', img: 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=300' },
            { index: 3, name: 'Funko Bronze', rarity: 'rare', img: 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=300' },
            { index: 4, name: 'Funko Classic', rarity: 'common', img: 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=300' }
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

let botUsername = "Funko_Stop_bot";

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
            
            // Simulating redirection for Telegram Sub
            if (taskId === 'tg_sub') {
                window.open('https://t.me/Funko_Stop', '_blank');
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
                    alert(data.message);
                }
            } catch (e) {
                console.error("Task claim error", e);
                // Fallback offline simulation
                const rewardPacks = taskId === 'order_2000' ? 3 : 1;
                userData.packs_count += rewardPacks;
                userData.completed_tasks.push(taskId);
                updateUI();
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
}

// Daily Pack Claiming
function setupDailyPackButton() {
    if (!dailyGiftBtn) return;
    
    // Check click on the floating button, navigate to tasks instead if user wants?
    // Wait, the user said "кнопку чтобы перекидывало на задания", but for the actual daily gift:
    dailyGiftBtn.addEventListener('click', async () => {
        
        // Let's check if it's ready first
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
                }
                alert(data.message || "Ежедневный пак пока недоступен.");
            }
        } catch (e) {
            // Local fallback simulation
            userData.packs_count++;
            userData.last_daily_pack = new Date().toISOString();
            updateUI();
            startDailyCountdown(86400);
        }
    });
}

function checkDailyTimer() {
    if (!userData.last_daily_pack) {
        dailyTimer.textContent = "ГОТОВО";
        dailyTimer.style.color = "#00ff88";
        dailyTimer.style.borderColor = "#00ff88";
        return;
    }

    const last = new Date(userData.last_daily_pack).getTime();
    const now = new Date().getTime();
    const diffSeconds = Math.floor((now - last) / 1000);

    if (diffSeconds >= 86400) {
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
    card3d.classList.remove('flipped');

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
                <img src="${drop.card.img}" class="card-character-image" alt="${drop.card.name}">
                <div class="card-character-name">${drop.card.name}</div>
                <div class="card-rarity-badge rarity-${drop.card.rarity}">${drop.card.rarity}</div>
            </div>
        `;

        setTimeout(() => {
            card3d.classList.add('flipped');

            if (window.confetti) {
                confetti({
                    particleCount: drop.card.rarity === 'legendary' ? 120 : 60,
                    spread: 70,
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
