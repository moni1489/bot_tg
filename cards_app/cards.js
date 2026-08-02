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
    packs_count: 3,
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
            { index: 2, name: 'Jesse Pinkman', rarity: 'common', img: '/cards/images/card_breaking_bad_2.png' },
            { index: 3, name: 'Saul Goodman', rarity: 'rare', img: '/cards/images/card_breaking_bad_3.png' },
            { index: 4, name: 'Gustavo Fring', rarity: 'epic', img: '/cards/images/card_breaking_bad_4.png' }
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
            { index: 1, name: 'Misa Amane', rarity: 'common',    img: '/cards/images/card_death_note_1.png' },
            { index: 2, name: 'Ryuk',       rarity: 'rare',      img: '/cards/images/card_death_note_2.png' },
            { index: 3, name: 'L',          rarity: 'epic',      img: '/cards/images/card_death_note_3.png' },
            { index: 4, name: 'Light Yagami', rarity: 'legendary', img: '/cards/images/card_death_note_4.png' }
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
    renderCollection();
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
            alert(`⏳ Ежедневный подарок уже получен! Следующий забор будет доступен через ${dailyTimer.textContent}.`);
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

// Pack Opening Flow — Shorts-style: card shoots from bottom of screen
let fastOpen = false;

openPackBtn.addEventListener('click', async () => {
    if (isOpening) return;
    if (userData.packs_count <= 0) {
        alert("У вас закончились паки! Заберите ежедневный пак или выполняйте задания.");
        return;
    }

    isOpening = true;
    const isFast = fastOpen;
    fastOpen = false; // reset for next time
    userData.packs_count--;
    updateUI();

    // Phase 1: Pack tearing and card peeking (No shaking)
    card3d.classList.remove('flipped', 'aura-epic', 'aura-legendary', 'card-fly-in');
    
    const packTop = document.getElementById('pack-top');
    const packInside = document.getElementById('pack-inside-card');

    boosterPack.classList.add('is-tearing');
    packTop.classList.add('tearing-left');
    packInside.classList.add('peeking');

    setTimeout(() => {
        // Phase 2: Pack disappears, prepare drop
        boosterPack.classList.add('hidden');

            const drop = rollRandomCard();
            const cardKey = `${drop.series.slug}_${drop.card.index}`;
            userCards[cardKey] = (userCards[cardKey] || 0) + 1;

        cardFront.innerHTML = `
            <div class="card-frame ${drop.series.theme}">
                <img src="${drop.card.img}" class="full-card-image" alt="${drop.card.name}" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
                <div class="card-fallback-frame" style="display:none; width:100%; height:100%; flex-direction:column; justify-content:space-between; align-items:center; padding:10px;">
                    <div class="card-series-tag">${drop.series.name}</div>
                    <div class="card-character-name">${drop.card.name}</div>
                    <div class="card-rarity-badge rarity-${drop.card.rarity}">${drop.card.rarity}</div>
                </div>
            </div>
        `;

        // Phase 3: Show card stage, card starts below screen
        cardStage.classList.remove('hidden');
        card3d.style.transform = 'translateY(120vh) rotateY(0deg)';
        card3d.style.transition = 'none';
        card3d.style.opacity = '0';
        
        // Reset and prepare background elements
        const svetBg = document.getElementById('svet-bg');
        const actions = document.getElementById('opened-card-actions');
        if(svetBg) svetBg.className = '';
        if(svetBg) svetBg.style.opacity = '0';
        cardStage.classList.remove('show-backcards');
        actions.classList.add('hidden');

        // Phase 4: Card SHOOTS UP from bottom
        requestAnimationFrame(() => {
                const flySpeed = isFast ? '0.8s' : '1.2s';
                card3d.style.transition = `transform ${flySpeed} cubic-bezier(0.22, 0.61, 0.36, 1), opacity 0.4s ease`;
                card3d.style.transform = 'translateY(0) rotateY(0deg)';
                card3d.style.opacity = '1';

                // Phase 5: Aura on arrival
                if (drop.card.rarity === 'epic') {
                    card3d.classList.add('aura-epic');
                } else if (drop.card.rarity === 'legendary') {
                    card3d.classList.add('aura-legendary');
                    // Extra screen flash for legendary
                    flashScreen();
                }
                
                // Phase 6: Wait for tap to flip
                const btnFlipCard = document.getElementById('btn-flip-card');
                
                const handleTapToFlip = () => {
                    btnFlipCard.removeEventListener('click', handleTapToFlip);
                    btnFlipCard.classList.add('hidden');
                    
                    const flipSpeed = isFast ? '0.7s' : '1.0s';
                    card3d.style.transition = `transform ${flipSpeed} cubic-bezier(0.175, 0.885, 0.32, 1.275), opacity 0.25s ease`;
                    card3d.style.transform = 'translateY(0) rotateY(180deg)';

                    // Confetti burst on reveal
                    setTimeout(() => {
                        if (window.confetti) {
                            confetti({
                                particleCount: drop.card.rarity === 'legendary' ? 200 : (drop.card.rarity === 'epic' ? 100 : 50),
                                spread: drop.card.rarity === 'legendary' ? 120 : 80,
                                origin: { y: 0.5 },
                                colors: drop.card.rarity === 'legendary' ? ['#ffc107','#ff9800','#fff'] :
                                        drop.card.rarity === 'epic' ? ['#e040fb','#9c27b0','#fff'] :
                                        drop.card.rarity === 'rare' ? ['#2196f3','#00bcd4','#fff'] :
                                        ['#4caf50','#8bc34a','#fff']
                            });
                        }

                        syncOpenedCard(drop.series.slug, drop.card.index);

                        // Show action buttons
                        actions.classList.remove('hidden');

                        const closeStageFn = (hideCompletely) => {
                            if (hideCompletely) {
                                cardStage.style.opacity = '0';
                                cardStage.style.visibility = 'hidden';
                                setTimeout(() => {
                                    cardStage.classList.add('hidden');
                                    cardStage.style.opacity = '';
                                    cardStage.style.visibility = '';
                                }, 400);
                            }
                            card3d.style.transition = 'none';
                            
                            cardStage.classList.remove('show-backcards');
                            if(svetBg) svetBg.style.opacity = '0';
                            actions.classList.add('hidden');
                            if (hideCompletely) {
                                boosterPack.classList.remove('hidden', 'shaking', 'is-tearing');
                                packTop.classList.remove('tearing-left');
                                packInside.classList.remove('peeking');
                                isOpening = false;
                                renderCollection();
                            }
                        };

                        const exitBtn = document.getElementById('btn-exit-pack');
                        const nextBtn = document.getElementById('btn-next-pack');
                        
                        // Clear old listeners
                        const newExit = exitBtn.cloneNode(true);
                        const newNext = nextBtn.cloneNode(true);
                        exitBtn.parentNode.replaceChild(newExit, exitBtn);
                        nextBtn.parentNode.replaceChild(newNext, nextBtn);
                        
                        newExit.addEventListener('click', () => {
                            card3d.style.transition = 'transform 0.4s ease, opacity 0.4s ease';
                            card3d.style.transform = 'translateY(-120vh) rotateY(180deg)';
                            card3d.style.opacity = '0';
                            setTimeout(() => closeStageFn(true), 400);
                        });
                        
                        newNext.addEventListener('click', () => {
                            if (userData.packs_count <= 0) {
                                alert("У вас больше нет паков!");
                                return;
                            }
                            card3d.style.transition = 'transform 0.4s ease, opacity 0.4s ease';
                            card3d.style.transform = 'translateY(120vh) rotateY(180deg)';
                            card3d.style.opacity = '0';
                            setTimeout(() => {
                                closeStageFn(false);
                                isOpening = false;
                                fastOpen = true; // Set flag for next open
                                document.getElementById('open-pack-btn').click();
                            }, 400);
                        });

                    }, isFast ? 200 : 350); // Small wait after flip starts before confetti
                };

                // Allow tapping to flip once the card has flown in
                setTimeout(() => {
                    btnFlipCard.classList.remove('hidden');
                    btnFlipCard.style.animation = 'fadeInSlideUp 0.4s ease forwards';
                    btnFlipCard.addEventListener('click', handleTapToFlip);
                }, isFast ? 500 : 900);
                
                // Trigger glow (backcards disabled as requested)
                if(svetBg) svetBg.classList.add('svet-' + drop.card.rarity);
                if(svetBg) svetBg.style.opacity = '1';
            });

    }, isFast ? 50 : 1500); // end of Phase 1 (Tearing duration)

});

function flashScreen() {
    const flash = document.createElement('div');
    flash.style.cssText = 'position:fixed;inset:0;background:#fff;z-index:9999;pointer-events:none;animation:flashAnim 0.5s ease forwards';
    const style = document.createElement('style');
    style.textContent = '@keyframes flashAnim{0%{opacity:0.9}100%{opacity:0}}';
    document.head.appendChild(style);
    document.body.appendChild(flash);
    setTimeout(() => { flash.remove(); style.remove(); }, 600);
}

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
const MANAGER_URL = 'https://t.me/Funko_Stop';

function openLightbox(card, series, isCollected) {
    const lb = document.getElementById('card-lightbox');
    const img = document.getElementById('lightbox-img');
    const name = document.getElementById('lightbox-name');
    const rarity = document.getElementById('lightbox-rarity');
    const seriesEl = document.getElementById('lightbox-series');

    if (!isCollected) {
        img.src = card.img;
        img.style.filter = 'brightness(0) saturate(0) contrast(1.2)';
        name.textContent = '???';
    } else {
        img.src = card.img;
        img.style.filter = 'none';
        name.textContent = card.name;
    }

    const rarityLabels = { common: 'Common', rare: 'Rare', epic: 'Epic', legendary: 'Legendary' };
    rarity.textContent = rarityLabels[card.rarity] || card.rarity;
    rarity.className = `card-lightbox-rarity lightbox-rarity-${card.rarity}`;
    seriesEl.textContent = series.name;

    lb.classList.remove('hidden');
}

function closeLightbox() {
    document.getElementById('card-lightbox').classList.add('hidden');
}

function setupLightboxClose() {
    document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
    document.getElementById('lightbox-close-btn').addEventListener('click', closeLightbox);
}

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

    // Update global header stats in DOM
    const elTotal = document.getElementById('stat-total');
    if (elTotal) elTotal.textContent = totalCards;
    const elCollected = document.getElementById('stat-collected');
    if (elCollected) elCollected.textContent = totalCollected;
    const elMissing = document.getElementById('stat-missing');
    if (elMissing) elMissing.textContent = missing;
    const elDupes = document.getElementById('stat-dupes');
    if (elDupes) elDupes.textContent = totalDupes;

    SERIES_CONFIG.forEach(series => {
        let seriesCollectedCount = 0;
        series.cards.forEach(c => { if ((userCards[`${series.slug}_${c.index}`] || 0) > 0) seriesCollectedCount++; });
        const isFull = seriesCollectedCount === 4;
        const progressPct = Math.round((seriesCollectedCount / 4) * 100);

        const seriesBlock = document.createElement('div');
        seriesBlock.className = 'series-card-block';

        // Title row
        const titleRow = document.createElement('div');
        titleRow.className = 'series-title-row';
        titleRow.innerHTML = `
            <div class="series-name">${series.name}</div>
            <div class="series-progress">${seriesCollectedCount} / 4</div>
        `;
        seriesBlock.appendChild(titleRow);

        // Card grid (4 columns)
        const grid = document.createElement('div');
        grid.className = 'cards-grid-view';

        series.cards.forEach(card => {
            const cardKey = `${series.slug}_${card.index}`;
            const count = userCards[cardKey] || 0;
            const isCollected = count > 0;

            const cardWrap = document.createElement('div');
            cardWrap.className = `coll-card rarity-border-${card.rarity} ${isCollected ? 'collected' : 'locked'}`;
            cardWrap.style.cursor = 'pointer';

            const imgContainer = document.createElement('div');
            imgContainer.className = 'coll-card-img-wrap';
            imgContainer.innerHTML = `
                <img src="${card.img}" 
                     class="coll-card-img ${isCollected ? '' : 'silhouette'}" 
                     alt="${card.name}"
                     onerror="this.src=''; this.style.display='none';">
                ${!isCollected ? '<div class="coll-lock-icon">🔒</div>' : ''}
                ${count > 1 ? `<div class="coll-dupe-badge">x${count}</div>` : ''}
            `;

            const label = document.createElement('div');
            label.className = 'coll-card-label';
            label.innerHTML = `
                <span class="coll-card-name ${isCollected ? '' : 'locked-name'}">${isCollected ? card.name : '???'}</span>
                <span class="coll-rarity-dot rarity-dot-${card.rarity}"></span>
            `;

            cardWrap.appendChild(imgContainer);
            cardWrap.appendChild(label);

            // Click to preview
            cardWrap.addEventListener('click', () => openLightbox(card, series, isCollected));

            grid.appendChild(cardWrap);
        });

        seriesBlock.appendChild(grid);

        // Progress bar
        const progressWrap = document.createElement('div');
        progressWrap.className = 'series-progress-wrap';
        progressWrap.innerHTML = `
            <div class="series-progress-bar-bg">
                <div class="series-progress-bar-fill ${isFull ? 'full' : ''}" style="width:${progressPct}%"></div>
            </div>
        `;
        seriesBlock.appendChild(progressWrap);

        // Prize banner when series is complete
        if (isFull) {
            const prizeBanner = document.createElement('div');
            prizeBanner.className = 'series-prize-banner';
            prizeBanner.innerHTML = `
                <div class="series-prize-text">🏆 Коллекция собрана!<br>Напишите менеджеру за призом!</div>
                <button class="series-prize-btn" onclick="window.open('${MANAGER_URL}','_blank')">Написать →</button>
            `;
            seriesBlock.appendChild(prizeBanner);
        }

        seriesContainer.appendChild(seriesBlock);
    });
}


function setupTestButton() {
    const addTestPacksBtn = document.getElementById('add-test-packs-btn');
    if (addTestPacksBtn) {
        addTestPacksBtn.addEventListener('click', async () => {
            userData.packs_count += 10;
            updateUI();
            if (window.confetti) {
                confetti({ particleCount: 40, spread: 60, origin: { y: 0.7 } });
            }
            try {
                const res = await fetch('/api/cards/give_test_packs', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: userData.telegram_id })
                });
                const data = await res.json();
                if (data.success) {
                    userData.packs_count = data.packs_count;
                    updateUI();
                }
            } catch (e) {
                console.log("Local test pack addition");
            }
        });
    }

    const resetTestDailyBtn = document.getElementById('reset-test-daily-btn');
    if (resetTestDailyBtn) {
        resetTestDailyBtn.addEventListener('click', async () => {
            userData.last_daily_pack = null;
            if (dailyTimerInterval) clearInterval(dailyTimerInterval);
            checkDailyTimer();
            try {
                await fetch('/api/cards/reset_daily_test', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: userData.telegram_id })
                });
            } catch (e) {}
        });
    }
}

function initApp() {
    setupNavigation();
    setupRefLink();
    setupTasks();
    setupDailyPackButton();
    setupTestButton();
    setupLightboxClose();
    fetchProfile();
}

initApp();
