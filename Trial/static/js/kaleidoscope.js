//based on the excellent kaleidoscope code by Nekodigi (https://gist.github.com/Nekodigi/b72a3f110ecca00d1c8b343fe58bf4e2)

let capture;
let r;//kaleidoscope radius
let angle;
let imgAngleOff = 0;//offset angle when uv mapping
let angularSpeed=0;
let w;
let h;
let showStroke=false;s
function setup() {
    createCanvas(800,800, WEBGL);
    angle=2*PI/20;
    w=width;
    h=height;
    capture = createCapture(VIDEO);
    capture.hide();
    r = width/2;
    noStroke();
    //  stroke(255);
    textureMode(NORMAL);
}

function draw() {
    background(0);

    beginShape(TRIANGLE_FAN);
    texture(capture);
    if(showStroke)  
        stroke(255);
    else
        noStroke();
    vertex(0, 0, 0.5, 0.5);
    let angleOff = 0;
    for (var i=0; i<TWO_PI/angle; i++) {
        let uvAngle1 = imgAngleOff;
        let uvAngle2 = angle+imgAngleOff;
        if (i%2 == 1) {
            uvAngle2 = imgAngleOff;
            uvAngle1 = angle+imgAngleOff;
        }
        vertex(cos(angleOff)*r, sin(angleOff)*r, 0.5+cos(uvAngle1)/2, 0.5+sin(uvAngle1)/2);
        vertex(cos(angle+angleOff)*r, sin(angle+angleOff)*r, 0.5+cos(uvAngle2)/2, 0.5+sin(uvAngle2)/2);
        angleOff += angle;
    }
    endShape();
    imgAngleOff += angularSpeed;
}

function mouseMoved()
{
    angle = 2*PI/map(mouseX, 0, width, 1, 40);
    angularSpeed=map(mouseY-height/2, 0, height/2, 0, .05);

}
function keyPressed()
{
    if(key=='s')
        showStroke=!showStroke;
}

function touchMoved()
{
    angle = 2*PI/map(mouseX, 0, width, 1, 40);
    angularSpeed=map(mouseY-height/2, 0, height/2, 0, .05);

}
