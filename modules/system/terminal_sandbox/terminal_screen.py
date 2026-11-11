"""Bounded text screen for common VT100/xterm controls; raw output stays on disk."""
import unicodedata
from collections import deque


class TerminalScreen:
    def __init__(self, columns=100, rows=30, history_limit=0):
        self.columns,self.rows=columns,rows
        self.cells=[[' ']*columns for _ in range(rows)]
        self.x=self.y=0
        self.saved=(0,0)
        self.top,self.bottom=0,rows-1
        self.state,self.sequence='text',''
        self.alternate=None
        self.wrap=True
        self.application_cursor=False
        self.pending_wrap=False
        self.queries=[]
        self.history=deque(maxlen=history_limit)

    def snapshot(self):
        return {'text':'\n'.join(''.join(row).rstrip() for row in self.cells).rstrip('\n'),
                'columns':self.columns,'rows':self.rows,'cursor':{'x':self.x,'y':self.y}}

    def transcript(self):
        return '\n'.join([*self.history,self.snapshot()['text']]).rstrip('\n')

    def resize(self, columns, rows):
        self.cells=[(row+[' ']*columns)[:columns] for row in self.cells[:rows]]
        self.cells.extend([[' ']*columns for _ in range(rows-len(self.cells))])
        self.columns,self.rows=columns,rows
        self.x,self.y=min(self.x,columns-1),min(self.y,rows-1)
        self.top,self.bottom=0,rows-1
        self.pending_wrap=False
        if self.alternate:
            cells,x,y=self.alternate
            cells=[(row+[' ']*columns)[:columns] for row in cells[:rows]]
            cells.extend([[' ']*columns for _ in range(rows-len(cells))])
            self.alternate=(cells,min(x,columns-1),min(y,rows-1))

    def linefeed(self):
        if self.y==self.bottom:
            removed=self.cells.pop(self.top)
            if self.top==0 and self.alternate is None:self.history.append(''.join(removed).rstrip())
            self.cells.insert(self.bottom,[' ']*self.columns)
        else:
            self.y=min(self.rows-1,self.y+1)
        self.pending_wrap=False

    def reverse(self):
        if self.y==self.top:
            self.cells.pop(self.bottom);self.cells.insert(self.top,[' ']*self.columns)
        else:
            self.y=max(0,self.y-1)

    def feed(self, text):
        for char in text:
            if self.state=='osc':
                if char=='\x07':self.state='text'
                elif char=='\x1b':self.state='string_escape'
                continue
            if self.state=='string_escape':
                self.state='text' if char=='\\' else 'osc'
                continue
            if self.state=='charset':
                self.state='text';continue
            if self.state=='escape':
                self.state='text'
                if char=='[':self.state='csi';self.sequence=''
                elif char in ']P_^':self.state='osc'
                elif char in '()*+':self.state='charset'
                elif char=='7':self.saved=(self.x,self.y)
                elif char=='8':self.x,self.y=self.saved
                elif char=='D':self.linefeed()
                elif char=='E':self.x=0;self.linefeed()
                elif char=='M':self.reverse()
                elif char=='c':self.__init__(self.columns,self.rows,self.history.maxlen)
                continue
            if self.state=='csi':
                if '@'<=char<='~':
                    self.control(char,self.sequence);self.state='text';self.sequence=''
                elif len(self.sequence)<256:self.sequence+=char
                else:self.state='text';self.sequence=''
                continue
            if char=='\x1b':self.state='escape'
            elif char=='\r':self.x=0;self.pending_wrap=False
            elif char in '\n\v\f':self.linefeed()
            elif char=='\b':self.x=max(0,self.x-1);self.pending_wrap=False
            elif char=='\t':self.x=min(self.columns-1,(self.x//8+1)*8);self.pending_wrap=False
            elif char>=' ' and char!='\x7f':self.put(char)

    def put(self, char):
        if unicodedata.combining(char):
            self.cells[self.y][max(0,self.x-1)]+=char;return
        width=2 if unicodedata.east_asian_width(char) in {'W','F'} else 1
        if self.pending_wrap or self.x+width>self.columns:
            if self.wrap:self.x=0;self.linefeed()
            self.pending_wrap=False
        self.cells[self.y][self.x]=char
        if width==2 and self.x+1<self.columns:self.cells[self.y][self.x+1]=''
        self.x+=width
        if self.x>=self.columns:self.x=self.columns-1;self.pending_wrap=self.wrap

    def control(self, final, sequence):
        private=sequence.startswith('?')
        raw=sequence.lstrip('?><!')
        try:values=[int(v) if v else 0 for v in raw.split(';')]
        except ValueError:return
        n=min(10000,values[0] or 1)
        if final in 'ABCDEFGHf`ade':self.pending_wrap=False
        if final=='A':self.y=max(0,self.y-n)
        elif final in 'Be':self.y=min(self.rows-1,self.y+n)
        elif final in 'Ca':self.x=min(self.columns-1,self.x+n)
        elif final=='D':self.x=max(0,self.x-n)
        elif final=='E':self.x=0;self.y=min(self.rows-1,self.y+n)
        elif final=='F':self.x=0;self.y=max(0,self.y-n)
        elif final in 'G`':self.x=min(self.columns-1,n-1)
        elif final=='d':self.y=min(self.rows-1,n-1)
        elif final in 'Hf':
            self.y=min(self.rows-1,max(0,(values[0] or 1)-1))
            self.x=min(self.columns-1,max(0,((values[1] if len(values)>1 else 1) or 1)-1))
        elif final=='J':
            mode=values[0]
            for y in range(self.rows):
                for x in range(self.columns):
                    if mode in {2,3} or (mode==0 and (y,x)>=(self.y,self.x)) or (mode==1 and (y,x)<=(self.y,self.x)):
                        self.cells[y][x]=' '
        elif final=='K':
            for x in range(self.columns):
                if values[0]==2 or (values[0]==0 and x>=self.x) or (values[0]==1 and x<=self.x):self.cells[self.y][x]=' '
        elif final=='X':
            self.cells[self.y][self.x:min(self.columns,self.x+n)]=[' ']*min(n,self.columns-self.x)
        elif final=='P':
            del self.cells[self.y][self.x:self.x+n];self.cells[self.y].extend([' ']*(self.columns-len(self.cells[self.y])))
        elif final=='@':
            self.cells[self.y][self.x:self.x]=[' ']*min(n,self.columns);self.cells[self.y]=self.cells[self.y][:self.columns]
        elif final in 'LM' and self.top<=self.y<=self.bottom:
            for _ in range(min(n,self.bottom-self.y+1)):
                self.cells.pop(self.bottom if final=='L' else self.y)
                self.cells.insert(self.y if final=='L' else self.bottom,[' ']*self.columns)
        elif final in 'ST':
            for _ in range(min(n,self.bottom-self.top+1)):
                self.cells.pop(self.top if final=='S' else self.bottom)
                self.cells.insert(self.bottom if final=='S' else self.top,[' ']*self.columns)
        elif final=='r' and not private:
            top=max(0,(values[0] or 1)-1);bottom=min(self.rows-1,(values[1] if len(values)>1 and values[1] else self.rows)-1)
            if top<bottom:self.top,self.bottom=top,bottom;self.x=self.y=0
        elif final=='s':self.saved=(self.x,self.y)
        elif final=='u':self.x,self.y=self.saved
        elif final in 'hl' and private:
            for value in values:
                if value in {47,1047,1049}:
                    if final=='h' and self.alternate is None:
                        self.alternate=(self.cells,self.x,self.y);self.cells=[[' ']*self.columns for _ in range(self.rows)];self.x=self.y=0
                    elif final=='l' and self.alternate is not None:
                        self.cells,self.x,self.y=self.alternate;self.alternate=None
                elif value==7:self.wrap=final=='h'
                elif value==1:self.application_cursor=final=='h'
        elif final=='n' and values[0]==6:self.queries.append(f'\x1b[{self.y+1};{self.x+1}R'.encode())
        elif final=='n' and values[0]==5:self.queries.append(b'\x1b[0n')
        elif final=='c':self.queries.append(b'\x1b[?1;2c')
        self.queries=self.queries[-8:]
