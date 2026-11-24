//! The LPeg matching machine (lpvm.c): a backtrack stack of choice and call
//! frames over the instruction array, recording captures as it goes.

use super::capture::*;
use super::code::*;
use super::tree::*;
use super::value::V;

/// Initial size of the backtrack stack and default `setmaxstack` limit.
pub const MAXBACK: usize = 400;
const NULL: usize = usize::MAX;
const GIVEUP: usize = usize::MAX;
const SHRT_MAX: usize = 32767;

#[derive(Clone, Copy)]
struct Frame {
    /// Subject position to backtrack to, or `NULL` for a call frame.
    s: usize,
    /// Instruction to continue at (the return address of a call frame).
    p: usize,
    caplevel: usize,
}

pub struct MatchArgs<'a> {
    pub subject: &'a [u8],
    /// The subject as the Lua string passed to `match`.
    pub subject_v: V,
    pub ktable: &'a [V],
    /// Extra arguments (`Carg`).
    pub args: &'a [V],
    pub max_stack: usize,
}

use std::mem::MaybeUninit;

const INLINE_FRAMES: usize = 32;
const INLINE_CAPS: usize = 16;

struct FrameStack {
    inline: [MaybeUninit<Frame>; INLINE_FRAMES],
    len: usize,
    heap: Vec<Frame>,
}

impl FrameStack {
    #[inline(always)]
    fn new() -> Self {
        Self {
            inline: [const { MaybeUninit::uninit() }; INLINE_FRAMES],
            len: 0,
            heap: Vec::new(),
        }
    }

    #[inline(always)]
    fn len(&self) -> usize {
        if self.heap.capacity() == 0 { self.len } else { self.heap.len() }
    }

    #[inline(always)]
    fn push(&mut self, frame: Frame) {
        if self.heap.capacity() == 0 {
            if self.len < INLINE_FRAMES {
                self.inline[self.len].write(frame);
                self.len += 1;
                return;
            }
            let mut heap = Vec::with_capacity(INLINE_FRAMES * 2);
            // SAFETY: `self.inline[..self.len]` are initialized `Copy` `Frame`s.
            let slice = unsafe {
                std::slice::from_raw_parts(self.inline.as_ptr().cast::<Frame>(), self.len)
            };
            heap.extend_from_slice(slice);
            heap.push(frame);
            self.len = 0;
            self.heap = heap;
        } else {
            self.heap.push(frame);
        }
    }

    #[inline(always)]
    fn pop(&mut self) -> Option<Frame> {
        if self.heap.capacity() == 0 {
            if self.len == 0 {
                None
            } else {
                self.len -= 1;
                // SAFETY: `self.inline[self.len]` was initialized when pushed.
                Some(unsafe { self.inline[self.len].assume_init() })
            }
        } else {
            self.heap.pop()
        }
    }

    #[inline(always)]
    fn last_mut(&mut self) -> Option<&mut Frame> {
        if self.heap.capacity() == 0 {
            if self.len == 0 {
                None
            } else {
                // SAFETY: `self.inline[self.len - 1]` was initialized when pushed.
                Some(unsafe { self.inline[self.len - 1].assume_init_mut() })
            }
        } else {
            self.heap.last_mut()
        }
    }
}

pub struct SmallCaps {
    inline: [MaybeUninit<Cap>; INLINE_CAPS],
    len: usize,
    heap: Vec<Cap>,
}

impl SmallCaps {
    #[inline(always)]
    fn new() -> Self {
        Self {
            inline: [const { MaybeUninit::uninit() }; INLINE_CAPS],
            len: 0,
            heap: Vec::new(),
        }
    }

    #[inline(always)]
    pub fn as_slice(&self) -> &[Cap] {
        if self.heap.capacity() == 0 {
            // SAFETY: `self.inline[..self.len]` are initialized `Copy` `Cap`s.
            unsafe { std::slice::from_raw_parts(self.inline.as_ptr().cast::<Cap>(), self.len) }
        } else {
            &self.heap
        }
    }

    #[inline(always)]
    fn as_mut_slice(&mut self) -> &mut [Cap] {
        if self.heap.capacity() == 0 {
            // SAFETY: `self.inline[..self.len]` are initialized `Copy` `Cap`s.
            unsafe {
                std::slice::from_raw_parts_mut(self.inline.as_mut_ptr().cast::<Cap>(), self.len)
            }
        } else {
            &mut self.heap
        }
    }

    #[inline(always)]
    fn len(&self) -> usize {
        if self.heap.capacity() == 0 { self.len } else { self.heap.len() }
    }

    #[inline(always)]
    fn push(&mut self, cap: Cap) {
        if self.heap.capacity() == 0 {
            if self.len < INLINE_CAPS {
                self.inline[self.len].write(cap);
                self.len += 1;
                return;
            }
            let mut heap = Vec::with_capacity(INLINE_CAPS * 2);
            let slice = unsafe {
                std::slice::from_raw_parts(self.inline.as_ptr().cast::<Cap>(), self.len)
            };
            heap.extend_from_slice(slice);
            heap.push(cap);
            self.len = 0;
            self.heap = heap;
        } else {
            self.heap.push(cap);
        }
    }

    #[inline(always)]
    fn truncate(&mut self, new_len: usize) {
        if self.heap.capacity() == 0 {
            if new_len < self.len {
                self.len = new_len;
            }
        } else {
            self.heap.truncate(new_len);
        }
    }
}

pub struct Matched {
    /// End of the match (0-based, exclusive).
    pub end: usize,
    pub caps: SmallCaps,
    pub dynvals: Vec<V>,
}

/// Run `code` on the subject from `init`; `None` when the pattern fails.
pub fn run(code: &[u32], init: usize, m: &MatchArgs<'_>, env: &mut dyn Env) -> LResult<Option<Matched>> {
    let subject = m.subject;
    let e = subject.len();
    let mut limit = MAXBACK;
    let mut stack = FrameStack::new();
    let mut caps = SmallCaps::new();
    let mut dynvals: Vec<V> = Vec::new();
    let mut s = init;
    let mut p = 0usize;
    stack.push(Frame { s, p: GIVEUP, caplevel: 0 });

    macro_rules! pushframe {
        ($frame:expr) => {{
            if stack.len() == limit {
                if limit >= m.max_stack {
                    return Err(env.error(format!(
                        "backtrack stack overflow (current limit is {})",
                        m.max_stack
                    )));
                }
                limit = (2 * limit).min(m.max_stack);
            }
            stack.push($frame);
        }};
    }

    'main: loop {
        'ok: {
            match op(code, p) {
                IEND => {
                    caps.push(Cap { s: 0, idx: 0, kind: CCLOSE, siz: 0 });
                    return Ok(Some(Matched { end: s, caps, dynvals }));
                }
                IRET => {
                    p = stack.pop().expect("return frame").p;
                    continue 'main;
                }
                IANY => {
                    if s < e {
                        p += 1;
                        s += 1;
                        continue 'main;
                    }
                }
                ITESTANY => {
                    if s < e {
                        p += 2;
                    } else {
                        p = (p as i64 + offset(code, p) as i64) as usize;
                    }
                    continue 'main;
                }
                ICHAR => {
                    if s < e && subject[s] == aux(code, p) {
                        p += 1;
                        s += 1;
                        continue 'main;
                    }
                }
                ITESTCHAR => {
                    if s < e && subject[s] == aux(code, p) {
                        p += 2;
                    } else {
                        p = (p as i64 + offset(code, p) as i64) as usize;
                    }
                    continue 'main;
                }
                ISET => {
                    if s < e && set_has(code, p + 1, subject[s]) {
                        p += CHARSETINSTSIZE;
                        s += 1;
                        continue 'main;
                    }
                }
                ITESTSET => {
                    if s < e && set_has(code, p + 2, subject[s]) {
                        p += 1 + CHARSETINSTSIZE;
                    } else {
                        p = (p as i64 + offset(code, p) as i64) as usize;
                    }
                    continue 'main;
                }
                IBEHIND => {
                    let n = aux(code, p) as usize;
                    if n <= s {
                        s -= n;
                        p += 1;
                        continue 'main;
                    }
                }
                ISPAN => {
                    while s < e && set_has(code, p + 1, subject[s]) {
                        s += 1;
                    }
                    p += CHARSETINSTSIZE;
                    continue 'main;
                }
                IJMP => {
                    p = (p as i64 + offset(code, p) as i64) as usize;
                    continue 'main;
                }
                ICHOICE => {
                    let target = (p as i64 + offset(code, p) as i64) as usize;
                    pushframe!(Frame { s, p: target, caplevel: caps.len() });
                    p += 2;
                    continue 'main;
                }
                ICALL => {
                    pushframe!(Frame { s: NULL, p: p + 2, caplevel: 0 });
                    p = (p as i64 + offset(code, p) as i64) as usize;
                    continue 'main;
                }
                ICOMMIT => {
                    stack.pop();
                    p = (p as i64 + offset(code, p) as i64) as usize;
                    continue 'main;
                }
                IPARTIALCOMMIT => {
                    let top = stack.last_mut().expect("choice frame");
                    top.s = s;
                    top.caplevel = caps.len();
                    p = (p as i64 + offset(code, p) as i64) as usize;
                    continue 'main;
                }
                IBACKCOMMIT => {
                    let f = stack.pop().expect("choice frame");
                    s = f.s;
                    caps.truncate(f.caplevel);
                    p = (p as i64 + offset(code, p) as i64) as usize;
                    continue 'main;
                }
                IFAILTWICE => {
                    stack.pop();
                }
                IFAIL => {}
                ICLOSERUNTIME => {
                    let close = caps.len();
                    caps.push(Cap { s, idx: 0, kind: CCLOSE, siz: 1 });
                    let open = findopen(caps.as_slice(), close);
                    let id = finddyncap(caps.as_slice(), open, close);
                    let (func, nested) = {
                        let mut cs = CapState {
                            caps: caps.as_slice(),
                            cap: open,
                            subject,
                            ktable: m.ktable,
                            dynvals: &dynvals,
                            args: m.args,
                            env,
                            stack: Vec::new(),
                        };
                        let func = match caps.as_slice()[open].idx {
                            0 => V::Nil,
                            idx => m.ktable[idx as usize - 1].clone(),
                        };
                        cs.pushnestedvalues(false)?;
                        (func, cs.stack)
                    };
                    let mut args = Vec::with_capacity(nested.len() + 2);
                    args.push(m.subject_v.clone());
                    args.push(V::Int(s as i64 + 1));
                    args.extend(nested);
                    let results = env.call(&func, &args)?;
                    if id > 0 {
                        dynvals.truncate(id - 1);
                    }
                    let open_s = caps.as_slice()[open].s;
                    caps.truncate(open);
                    // resdyncaptures
                    let res = match results.first() {
                        None => None,
                        Some(first) if !first.truthy() => None,
                        Some(V::Bool(_)) => Some(s),
                        Some(first) => {
                            let pos = first.to_integer().unwrap_or(0) - 1;
                            if pos < s as i64 || pos > e as i64 {
                                return Err(env.error(
                                    "invalid position returned by match-time capture".to_string(),
                                ));
                            }
                            Some(pos as usize)
                        }
                    };
                    let Some(res) = res else {
                        break 'ok;
                    };
                    s = res;
                    let n = results.len() - 1;
                    if n > 0 {
                        let fr = dynvals.len();
                        if fr + n >= SHRT_MAX {
                            return Err(env.error("too many results in match-time capture".to_string()));
                        }
                        dynvals.extend(results.into_iter().skip(1));
                        caps.push(Cap { s: open_s, idx: 0, kind: CGROUP, siz: 0 });
                        for i in 1..=n {
                            caps.push(Cap { s, idx: (fr + i) as u16, kind: CRUNTIME, siz: 1 });
                        }
                        caps.push(Cap { s, idx: 0, kind: CCLOSE, siz: 1 });
                    }
                    p += 1;
                    continue 'main;
                }
                ICLOSECAPTURE => {
                    let s1 = s;
                    let last = caps.len() - 1;
                    // if possible, turn the capture into a full capture
                    let last_cap = &mut caps.as_mut_slice()[last];
                    if last_cap.siz == 0 && s1 - last_cap.s < u8::MAX as usize {
                        last_cap.siz = (s1 - last_cap.s + 1) as u8;
                        p += 1;
                        continue 'main;
                    }
                    caps.push(Cap { s, idx: key(code, p), kind: getkind(aux(code, p)), siz: 1 });
                    p += 1;
                    continue 'main;
                }
                IOPENCAPTURE => {
                    caps.push(Cap { s, idx: key(code, p), kind: getkind(aux(code, p)), siz: 0 });
                    p += 1;
                    continue 'main;
                }
                IFULLCAPTURE => {
                    let off = getoff(aux(code, p)) as usize;
                    caps.push(Cap {
                        s: s - off,
                        idx: key(code, p),
                        kind: getkind(aux(code, p)),
                        siz: off as u8 + 1,
                    });
                    p += 1;
                    continue 'main;
                }
                opcode => unreachable!("lpeg vm: opcode {opcode}"),
            }
        }
        // the pattern failed: backtrack to the last choice
        let f = loop {
            let f = stack.pop().expect("base frame");
            if f.s != NULL {
                break f;
            }
        };
        if f.p == GIVEUP {
            return Ok(None);
        }
        if !dynvals.is_empty() {
            let id = finddyncap(caps.as_slice(), f.caplevel, caps.len());
            if id > 0 {
                dynvals.truncate(id - 1);
            }
        }
        caps.truncate(f.caplevel);
        s = f.s;
        p = f.p;
    }
}

#[inline]
fn set_has(code: &[u32], at: usize, c: u8) -> bool {
    code[at + (c >> 5) as usize] >> (c & 31) & 1 != 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_stack_spill_and_drain_does_not_resurrect_inline_frames() {
        let mut stack = FrameStack::new();
        let total = INLINE_FRAMES + 8;
        for i in 0..total {
            stack.push(Frame { s: i, p: i * 10, caplevel: i });
        }
        assert_eq!(stack.len(), total);
        for i in (0..total).rev() {
            let f = stack.pop().unwrap();
            assert_eq!((f.s, f.p, f.caplevel), (i, i * 10, i));
        }
        assert_eq!(stack.len(), 0);
        assert!(stack.pop().is_none());
        assert!(stack.last_mut().is_none());

        // Subsequent pushes after draining must stay coherent on heap.
        stack.push(Frame { s: 99, p: 100, caplevel: 1 });
        assert_eq!(stack.len(), 1);
        assert_eq!(stack.last_mut().unwrap().s, 99);
        let f = stack.pop().unwrap();
        assert_eq!(f.s, 99);
        assert_eq!(stack.len(), 0);
    }

    #[test]
    fn small_caps_spill_and_truncate_zero_does_not_resurrect_inline_caps() {
        let mut caps = SmallCaps::new();
        let total = INLINE_CAPS + 8;
        for i in 0..total {
            caps.push(Cap { s: i, idx: i as u16, kind: CSIMPLE, siz: 1 });
        }
        assert_eq!(caps.len(), total);
        assert_eq!(caps.as_slice().len(), total);

        caps.truncate(0);
        assert_eq!(caps.len(), 0);
        assert!(caps.as_slice().is_empty());
        assert!(caps.as_mut_slice().is_empty());

        caps.push(Cap { s: 77, idx: 3, kind: CSIMPLE, siz: 1 });
        assert_eq!(caps.len(), 1);
        assert_eq!(caps.as_slice()[0].s, 77);
    }
}
